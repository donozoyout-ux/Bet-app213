"""Read-only quote validation and draft settlement; never places or records bets."""
from copy import deepcopy
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


class DraftSelection(BaseModel):
    match_id: int = Field(gt=0)
    recommendation_id: str = Field(max_length=100)
    bookmaker: Literal['Crown','Bet365','Sbobet'] = 'Bet365'


class DraftRequest(BaseModel):
    selections: list[DraftSelection] = Field(min_length=1,max_length=2)

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
        for row in await session.execute(select(model,Bookmaker.name,model.raw['_field_observed_at'],model.raw['_field_lines']).options(defer(model.raw)).join(Bookmaker).where(model.match_id.in_(ids),Bookmaker.name.in_(['Crown','Bet365','Sbobet']))):
            row[0]._matching_meta={'_field_observed_at':row[2] or {},'_field_lines':row[3] or {}}
            rows[model][row[0].match_id].append((row[0],row[1]))
    for id,recommendations in groups.items():
        enrich_recommendations_with_odds(recommendations,id,'scheduled',rows[Odds1X2][id],rows[AsianTotals][id],rows[AsianHandicap][id],rows[MarketOdds][id],cutoff=now)


async def quote_draft(session, request, now):
    items=[]
    for choice in request.selections:
        match=await session.get(Match,choice.match_id)
        item=dict(match_id=choice.match_id,recommendation_id=choice.recommendation_id,bookmaker=choice.bookmaker,eligible=False,reason='selection_unavailable',price=None,observed_at=None)
        if match is None or not await session.scalar(select(League.enabled).where(League.id==match.league_id)):items.append(item);continue
        if match.status!='scheduled' or not match.kickoff_at or aware(match.kickoff_at)<=now:
            item['reason']='match_started';items.append(item);continue
        board=await session.get(PredictionBoard,match.league_id)
        if board is None or board.model_version!=MODEL_VERSION or aware(board.generated_at)>now or aware(board.expires_at)<=now:
            item['reason']='analysis_stale';items.append(item);continue
        record=next((r for r in board.payload['items'] if r['match']['id']==match.id),None)
        rec=next((r for r in record['recommendations'][:2] if r['id']==choice.recommendation_id and r.get('qualifies') is True),None) if record else None
        if rec is None:items.append(item);continue
        rec=deepcopy(rec)
        await current_odds(session,{match.id:[rec]},now)
        vo=rec['verified_odds'];book=vo.get('bookmakers',{}).get(choice.bookmaker,{})
        item.update(market=rec['market'],selection=rec['selection'],period=vo['period'],line=vo.get('line'),price=book.get('current_odds'),observed_at=book.get('observed_at'),expires_at=book.get('expires_at'),freshness=book.get('freshness','unknown'),probability=rec['probability'],confidence=rec['confidence'],label=rec['label'])
        item['eligible']=bool(book.get('coupon_eligible'))
        item['reason']=None if item['eligible'] else 'odds_stale' if book.get('current_odds') else 'no_verified_odds'
        items.append(item)
    eligible=all(item['eligible'] for item in items)
    total=1
    for item in items:
        if item['eligible']:total*=item['price']
    # Quotes from distinct bookmakers are a mathematical draft, not one offered bet.
    return dict(items=items,eligible=eligible,combined_odds=round(total,6) if eligible else None,mixed_bookmakers=len({i['bookmaker'] for i in items})>1,checked_at=now)


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
    items=[]
    for choice in request.selections:
        pick=draft_pick(choice.recommendation_id)
        match=await session.get(Match,choice.match_id)
        stats=await session.get(MatchStatistics,choice.match_id)
        outcome,actual=settle(match,stats,pick) if pick and match else ('awaiting_data',None)
        items.append(dict(match_id=choice.match_id,recommendation_id=choice.recommendation_id,status=outcome,actual=actual,match_status=match.status if match else None))
    return dict(items=items)
