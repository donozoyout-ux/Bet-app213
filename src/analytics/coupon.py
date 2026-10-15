"""Read-only quote validation and draft settlement; never places or records bets."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal,localcontext
import math
import re

from pydantic import BaseModel, Field, model_validator
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import defer
from src.models import Match, MatchStatistics, PredictionBoard, Bookmaker, Odds1X2, AsianTotals, AsianHandicap, MarketOdds, League
from src.jobs.worker import aware
from src.analytics.odds_matching import enrich_recommendations_with_odds
from src.analytics.board_cache import MODEL_VERSION
from src.analytics.performance import settle
from src.analytics.recommendations import qualified_record


def coupon_qualified(rec):
    """Both coupon modes retain the daily evidence floor; prices have no upper cap."""
    return qualified_record(rec) and rec['probability'] >= .70 and rec['reliability'] >= .80 and rec['score'] >= .80


class DraftSelection(BaseModel):
    match_id: int = Field(gt=0)
    recommendation_id: str = Field(max_length=100)
    bookmaker: Literal['Crown','Bet365','Sbobet'] = 'Bet365'


class DraftRequest(BaseModel):
    selections: list[DraftSelection] = Field(min_length=1)

    @model_validator(mode='after')
    def different_matches(self):
        if len({s.match_id for s in self.selections})!=len(self.selections):
            raise ValueError('Each draft selection must be from a different match')
        if any(draft_pick(s.recommendation_id) is None for s in self.selections):
            raise ValueError('Unsupported model selection')
        return self


async def current_odds(session, groups, now):
    """Four bounded reads for all fixtures, without running prediction models."""
    ids=list(groups)
    if not ids:return
    rows={}
    for model in (Odds1X2,AsianTotals,AsianHandicap,MarketOdds):
        rows[model]={id:[] for id in ids}
        for row in await session.execute(select(model,Bookmaker.name,model.raw['_field_observed_at'],model.raw['_field_lines']).options(defer(model.raw)).join(Bookmaker).where(model.match_id.in_(ids))):
            row[0]._matching_meta={'_field_observed_at':row[2],'_field_lines':row[3]}
            rows[model][row[0].match_id].append((row[0],row[1]))
    for id,recommendations in groups.items():
        enrich_recommendations_with_odds(recommendations,id,'scheduled',rows[Odds1X2][id],rows[AsianTotals][id],rows[AsianHandicap][id],rows[MarketOdds][id],cutoff=now)


async def quote_draft(session, request, now):
    ids=[c.match_id for c in request.selections]
    matches={r[0].id:r for r in await session.execute(select(Match,League.enabled).options(defer(Match.raw)).join(League).where(Match.id.in_(ids)))}
    boards={b.league_id:b for b in await session.scalars(select(PredictionBoard).where(PredictionBoard.league_id.in_({r[0].league_id for r in matches.values()})))}
    groups={};items=[]
    for choice in request.selections:
        entry=dict(match_id=choice.match_id,recommendation_id=choice.recommendation_id,bookmaker=choice.bookmaker,eligible=False,reason='selection_unavailable',price=None,observed_at=None)
        items.append(entry);row=matches.get(choice.match_id)
        if row is None or not row[1]:continue
        match=row[0]
        if match.status!='scheduled' or not match.kickoff_at or aware(match.kickoff_at)<=now:entry['reason']='match_started';continue
        if aware(match.kickoff_at)>now+timedelta(days=7):entry['reason']='outside_manual_window';continue
        board=boards.get(match.league_id)
        if board is None or board.model_version!=MODEL_VERSION or aware(board.generated_at)>now or aware(board.expires_at)<=now:entry['reason']='analysis_stale';continue
        from src.analytics.daily_pool import board_candidates
        candidates,_=board_candidates(board,match.id,match.kickoff_at,require_context=True)
        rec=next((r for r in candidates if r['id']==choice.recommendation_id and coupon_qualified(r)),None)
        if rec:groups[match.id]=[deepcopy(rec)]
    await current_odds(session,groups,now)
    for choice,entry in zip(request.selections,items):
        if choice.match_id not in groups:continue
        rec=groups[choice.match_id][0];vo=rec['verified_odds'];book=vo.get('bookmakers',{}).get(choice.bookmaker,{})
        entry.update(market=rec['market'],selection=rec['selection'],period=vo['period'],line=vo.get('line'),price=book.get('current_odds'),observed_at=book.get('observed_at'),expires_at=book.get('expires_at'),freshness=book.get('freshness','unknown'),probability=rec['probability'],confidence=rec['confidence'],reliability=rec['reliability'],score=rec['score'],label=rec['label'],eligible=bool(book.get('coupon_eligible')))
        entry['reason']=None if entry['eligible'] else 'odds_stale' if book.get('current_odds') else 'no_verified_odds'
    eligible=all(i['eligible'] for i in items)
    return dict(items=items,eligible=eligible,combined_odds=combined_price([i['price'] for i in items]) if eligible else None,mixed_bookmakers=len({i['bookmaker'] for i in items})>1,checked_at=now)


def combined_price(prices):
    with localcontext() as context:
        context.prec=34
        total=Decimal(1)
        for price in prices:total*=Decimal(str(price))
        value=float(total)
        return round(value,6) if math.isfinite(value) else str(total.normalize())


def draft_pick(id):
    """Decode an existing model selection ID; no probability/quality claim is made."""
    markets=['first_half_goals','second_half_goals','asian_handicap','double_chance','yellow_cards','red_cards','corners','cards','result','goals','btts']
    market=next((m for m in markets if id.endswith('_'+m)),None)
    if market is None:return None
    selection=id[:-(len(market)+1)]
    patterns={'result':r'home|draw|away','double_chance':r'1x|x2|12','btts':r'yes|no','asian_handicap':r'(home|away)_(plus|minus)_\d{1,2}_[05]'}
    if not re.fullmatch(patterns.get(market,r'(over|under)_\d{1,2}_[05]'),selection):return None
    return dict(id=id,market=market,selection=selection)


async def settle_draft(session, request):
    ids=[c.match_id for c in request.selections]
    matches={m.id:m for m in await session.scalars(select(Match).options(defer(Match.raw)).where(Match.id.in_(ids)))}
    stats={s.match_id:s for s in await session.scalars(select(MatchStatistics).options(defer(MatchStatistics.raw)).where(MatchStatistics.match_id.in_(ids)))}
    items=[]
    for choice in request.selections:
        pick=draft_pick(choice.recommendation_id);match=matches.get(choice.match_id)
        outcome,actual=settle(match,stats.get(choice.match_id),pick) if pick and match else ('awaiting_data',None)
        items.append(dict(match_id=choice.match_id,recommendation_id=choice.recommendation_id,status=outcome,actual=actual,match_status=match.status if match else None))
    return dict(items=items)
