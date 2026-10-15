"""Full daily fixture census, independent of the paginated display recommendations."""
from collections import Counter
from copy import deepcopy
from datetime import datetime
from math import isfinite

from sqlalchemy import select
from sqlalchemy.orm import defer
from src.models import League, Match, PredictionBoard
from src.jobs.worker import aware
from src.match_views import day_bounds, DISPLAY_TIMEZONE
from zoneinfo import ZoneInfo
from src.analytics.board_cache import MODEL_VERSION


def quality_failures(candidate):
    reasons=list(candidate.get('rejection_reasons',[]))
    if candidate.get('qualifies') is not True:reasons.append('general_model_quality')
    for field,minimum in [('probability',.70),('reliability',.80),('score',.80)]:
        value=candidate.get(field)
        if type(value) not in (float,int) or not isfinite(value) or value<minimum:
            reasons.append(f'coupon_{field}')
    return sorted(set(reasons))


def board_candidates(board, match_id, kickoff=None, require_context=False):
    """Worker-published ranked candidates; legacy boards expose only known evidence.

    Never reconstruct probabilities or mark omitted legacy markets as evaluated.
    Require the current kickoff to prevent a rescheduled fixture using an old board.
    """
    full=board.payload.get('coupon_records')
    records=full if full is not None else board.payload.get('items',[])
    record=next((r for r in records if r['match']['id']==match_id),None)
    if require_context and record is None:return [],False
    if record and kickoff is not None:
        if aware(datetime.fromisoformat(record['match']['kickoff_at']))!=aware(kickoff):return [],False
    if full is not None:
        return deepcopy(record.get('candidates',[])) if record else [],record is not None
    known={r['id']:r for r in next((r.get('candidates',[]) for r in board.payload.get('engine_analysis',[]) if r['match_id']==match_id),[])}
    if record:known.update({r['id']:r for r in record.get('recommendations',[])})
    return deepcopy(list(known.values())),False


async def daily_pool(session, now):
    from src.api.routes import match_query,match_response
    from src.analytics.coupon import current_odds,coupon_qualified
    today=now.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).date()
    start,end=day_bounds(today,DISPLAY_TIMEZONE)
    query,_,_=match_query()
    # No LIMIT, priced_only, display recommendation limit, or HTTP pagination.
    rows=(await session.execute(query.options(defer(Match.raw)).where(League.enabled.is_(True),Match.kickoff_at>=start,Match.kickoff_at<end).order_by(Match.kickoff_at,Match.id))).all()
    leagues={r[0].league_id for r in rows}
    boards={b.league_id:b for b in await session.scalars(select(PredictionBoard).where(PredictionBoard.league_id.in_(leagues)))}
    records=[];groups={};states=[];stamps=[];missing=[];stale=[]
    for row in rows:
        match=row[0];context={**match_response(row),'home_team_id':row[0].home_team_id,'away_team_id':row[0].away_team_id};board=boards.get(match.league_id)
        entry=dict(match=context,reason=None,analysis_generated_at=None,candidate_count=0,quality_candidates=0,legacy_candidate_coverage=False)
        states.append(entry)
        if match.status!='scheduled' or aware(match.kickoff_at)<=now:entry['reason']='match_started_or_unavailable';continue
        if board is None or board.model_version!=MODEL_VERSION:
            entry['reason']='analysis_missing';missing.append(match.league_id);continue
        entry['analysis_generated_at']=aware(board.generated_at)
        stamps.append(aware(board.generated_at))
        if aware(board.generated_at)>now or aware(board.expires_at)<=now:
            entry['reason']='analysis_stale';stale.append(match.league_id);continue
        candidates,complete=board_candidates(board,match.id,match.kickoff_at)
        entry['legacy_candidate_coverage']=not complete
        entry['market_evidence']=next((r.get('market_engines',{}) for r in board.payload.get('engine_analysis',[]) if r['match_id']==match.id),{})
        entry['sample_gaps']={market:data.get('model',{}).get('insufficient_reasons',[]) for mid,markets in board.payload.get('availability',[]) if mid==match.id for market,data in markets.items() if data.get('model',{}).get('insufficient_reasons')}
        entry['candidate_count']=len(candidates)
        entry['quality_candidates']=sum(coupon_qualified(r) for r in candidates)
        entry['quality_failures']=dict(Counter(reason for c in candidates for reason in quality_failures(c)))
        entry['quality_metrics']=[dict(id=c['id'],qualifies=c.get('qualifies'),**{
            field:c.get(field) if type(c.get(field)) in (int,float) and isfinite(c[field]) else None
            for field in ('probability','reliability','score')}) for c in candidates]
        if not candidates:
            analysis=next((r for r in board.payload.get('engine_analysis',[]) if r['match_id']==match.id),None)
            entry['reason']='insufficient_prematch_data' if analysis else 'analysis_missing'
        elif not entry['quality_candidates']:entry['reason']='quality_rejected'
        bound,_=board_candidates(board,match.id,match.kickoff_at,require_context=True)
        records.append(dict(match=context,recommendations=candidates,coupon_candidates=bound))
        groups[match.id]=candidates
    # Refresh source prices for every known candidate, including below-threshold ones,
    # so price coverage and quality rejections remain separate measurements.
    await current_odds(session,groups,now)
    from src.analytics.odds_diagnostics import report
    diagnostics=report(records,now)
    from src.analytics.odds_diagnostics import source_report
    await source_report(session,now,[r[0].id for r in rows])
    diagnostics['last_quality_price_observed_at']=report([{**r,'recommendations':[c for c in r['recommendations'] if coupon_qualified(c)]} for r in records],now)['last_price_observed_at']
    diagnostics['source_coverage']=session.info['source_coverage']
    diagnostics['last_price_observed_at']=session.info['source_coverage']['last_price_observed_at']
    for entry in states:
        candidates=groups.get(entry['match']['id'],[])
        record=next((r for r in records if r['match']['id']==entry['match']['id']),None)
        bound_ids={c['id'] for c in record['coupon_candidates']} if record else set()
        strong=[c for c in candidates if c['id'] in bound_ids and coupon_qualified(c)]
        fresh=[c for c in strong if c.get('verified_odds',{}).get('coupon_eligible')]
        entry['eligible_candidates']=len(fresh)
        entry['price_rejections']=dict(Counter(c.get('verified_odds',{}).get('reason') or 'no_verified_odds' for c in strong if not c.get('verified_odds',{}).get('coupon_eligible')))
        if entry['reason'] is None:
            entry['reason']='analysis_missing' if entry['quality_candidates'] and not bound_ids else 'eligible' if fresh else 'price_rejected'
    reasons=Counter(e['reason'] for e in states)
    diagnostics.update(coupon_date=today.isoformat(),total_matches=len(rows),inspected_matches=len(states),
        analyzed_matches=sum(e['analysis_generated_at'] is not None for e in states),
        fresh_analysis_matches=sum(e['reason'] not in ('analysis_missing','analysis_stale','match_started_or_unavailable') for e in states),
        quality_matches=sum(e['quality_candidates']>0 for e in states),eligible_matches=reasons['eligible'],
        match_rejections={k:v for k,v in reasons.items() if k!='eligible'},matches=states,
        last_analysis_generated_at=max((s for s in stamps if s<=now),default=None),
        candidate_coverage_complete=all(not e['legacy_candidate_coverage'] for e in states if e['reason'] not in ('analysis_missing','analysis_stale','match_started_or_unavailable')))
    session.info['prediction_snapshot']=dict(stale=bool(missing or stale),missing_leagues=sorted(set(missing)),stale_leagues=sorted(set(stale)),latest_generated_at=diagnostics['last_analysis_generated_at'])
    session.info['coupon_diagnostics']=diagnostics
    return records
