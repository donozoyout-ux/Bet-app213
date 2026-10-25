"""Bounded, read-only kickoff cohorts. Descriptive change is not model learning."""
from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import math
from sqlalchemy import select
from sqlalchemy.orm import defer
from src.models import PredictionSnapshot, Match, MatchStatistics, League, utcnow
from src.analytics.performance import display_result, wilson_interval
from src.analytics.recommendations import market_matches
from src.analytics.settlements import decisions_many
from src.jobs.worker import aware

ISTANBUL=ZoneInfo('Europe/Istanbul')
MIN_DECIDED=30
MIN_MATCHES=30
MAX_SNAPSHOTS=5000


def summarize(rows):
    counts=Counter(r['outcome'] for r in rows)
    decided=[r for r in rows if r['outcome'] in {'won','lost'}]
    n=len(decided);won=counts['won'];clusters=Counter(r['match_id'] for r in decided)
    effective=n*n/sum(v*v for v in clusters.values()) if n else 0
    rate=won/n if n else None
    radius=math.sqrt(math.log(40)/(2*effective)) if effective else None
    valid=[r for r in decided if isinstance(r['probability'],(int,float)) and not isinstance(r['probability'],bool) and math.isfinite(r['probability']) and 0<=r['probability']<=1]
    mean=sum(r['probability'] for r in valid)/len(valid) if valid else None
    observed=sum(r['outcome']=='won' for r in valid)/len(valid) if valid else None
    unresolved=sum(counts[k] for k in ('pending','awaiting_data','disputed'))
    enough=n>=MIN_DECIDED and len(clusters)>=MIN_MATCHES and effective>=MIN_MATCHES
    return dict(late_recorded=sum(r.get('late_recorded') is True for r in rows),settlement_timestamp_available=sum(r.get('late_recorded') is not None for r in rows),total=len(rows),decided=n,won=won,lost=counts['lost'],pending=counts['pending'],awaiting_data=counts['awaiting_data'],disputed=counts['disputed'],void=counts['void'],distinct_matches=len(clusters),effective_matches=effective,success_rate=rate,wilson_95=wilson_interval(won,n),cluster_95=dict(lower=max(0,rate-radius),upper=min(1,rate+radius)) if n else None,probability_valid=len(valid),mean_probability=mean,calibration_observed=observed,calibration_gap=observed-mean if valid else None,brier_score=sum((r['probability']-int(r['outcome']=='won'))**2 for r in valid)/len(valid) if valid else None,incomplete=bool(unresolved),sufficient=enough,chart_rate=rate,model_versions=sorted({r['model_version'] for r in rows}))


def build_trends(rows, as_of, *, truncated=False, duplicate_picks_excluded=0):
    lower=as_of-timedelta(days=60)
    rows=[r for r in rows if lower<=r['day']<as_of]
    def period(start,end):
        selected=[r for r in rows if start<=r['day']<end]
        result=dict(start=start.isoformat(),end_exclusive=end.isoformat(),**summarize(selected))
        result['breakdowns']={key:[dict(name=name,**summarize([r for r in selected if r[key]==name])) for name in sorted({r[key] for r in selected})] for key in ('league','market','model_version')}
        return result
    comparisons=[]
    for days in (7,30):
        previous=period(as_of-timedelta(days=2*days),as_of-timedelta(days=days))
        recent=period(as_of-timedelta(days=days),as_of)
        eligible=all(p['sufficient'] and not p['incomplete'] for p in (previous,recent)) and not truncated
        difference=recent['success_rate']-previous['success_rate'] if all(p['decided'] for p in (previous,recent)) else None
        direction='insufficient'
        if eligible:
            direction='increase' if recent['cluster_95']['lower']>previous['cluster_95']['upper'] else 'decrease' if recent['cluster_95']['upper']<previous['cluster_95']['lower'] else 'uncertain'
        comparisons.append(dict(days=days,recent=recent,previous=previous,eligible=eligible,raw_difference=difference,direction=direction))
    daily=[dict(date=(lower+timedelta(days=i)).isoformat(),**summarize([r for r in rows if r['day']==lower+timedelta(days=i)])) for i in range(60)]
    weekly=[]
    # Monday-based calendar weeks, clipped to the bounded observation window.
    cursor=lower
    while cursor<as_of:
        stop=min(cursor+timedelta(days=7-cursor.weekday()),as_of)
        weekly.append(dict(date=cursor.isoformat(),end_exclusive=stop.isoformat(),
                           **summarize([r for r in rows if cursor<=r['day']<stop])))
        cursor=stop
    active=[d for d in daily if d['total']]
    aggregation='daily' if len(active)>=14 and sum(d['decided']>=5 for d in active)>=len(active)*.7 else 'weekly'
    if truncated:
        for d in daily+weekly:d['chart_rate']=None
    return dict(as_of=as_of.isoformat(),timezone='Europe/Istanbul',cohort_basis='match_kickoff_date',today_excluded=True,min_decided=MIN_DECIDED,min_distinct_matches=MIN_MATCHES,min_effective_matches=MIN_MATCHES,truncated=truncated,duplicate_picks_excluded=duplicate_picks_excluded,first_observed_kickoff=min((r['day'] for r in rows),default=None),model_versions=sorted({r['model_version'] for r in rows}),model_update_verified=False,improvement_proven=False,daily=daily,weekly=weekly,aggregation=aggregation,comparisons=comparisons)


async def trends(session,as_of,league=None,market='all',exact_market=None,confidence=None,date_from=None,date_to=None):
    start=datetime.combine(as_of-timedelta(days=60),time(),ISTANBUL)
    end=datetime.combine(as_of,time(),ISTANBUL)
    query=select(PredictionSnapshot,Match,MatchStatistics,League.name).join(Match,Match.id==PredictionSnapshot.match_id).join(League,League.id==Match.league_id).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id).options(defer(Match.raw),defer(MatchStatistics.raw)).where(Match.kickoff_at>=start,Match.kickoff_at<end).order_by(Match.kickoff_at,Match.id).limit(MAX_SNAPSHOTS+1)
    if league:query=query.where(League.id==int(league)) if league.isdigit() else query.where(League.name.ilike('%'+league+'%'))
    if date_from:query=query.where(PredictionSnapshot.captured_at>=datetime.combine(date_from,time(),ISTANBUL))
    if date_to:query=query.where(PredictionSnapshot.captured_at<datetime.combine(date_to+timedelta(days=1),time(),ISTANBUL))
    records=(await session.execute(query)).all();truncated=len(records)>MAX_SNAPSHOTS;records=records[:MAX_SNAPSHOTS]
    frozen=await decisions_many(session,[r[1].id for r in records]) if records else {}
    rows=[];duplicates=0
    for snapshot,match,stats,league_name in records:
        if aware(snapshot.captured_at)>=aware(match.kickoff_at):continue
        seen=set()
        for index,pick in enumerate(snapshot.picks):
            identity=(pick.get('market'),pick.get('selection'))
            if identity in seen:duplicates+=1;continue
            seen.add(identity)
            if not market_matches([pick],market) or exact_market and pick.get('id')!=exact_market or confidence and pick.get('confidence')!=confidence:continue
            saved=frozen.get(match.id,{}).get(index)
            outcome,_,_=display_result(snapshot,match,stats,pick,saved)
            # A score-only retrospective evaluation is not a persisted result.
            # Reconciliation still excludes disputed/missing/void ledger entries.
            if not saved and outcome in {'won','lost'}:outcome='awaiting_data'
            kickoff_day=aware(match.kickoff_at).astimezone(ISTANBUL).date()
            late=aware(saved.recorded_at).astimezone(ISTANBUL).date()>kickoff_day if saved else None
            rows.append(dict(day=kickoff_day,late_recorded=late,match_id=match.id,league=league_name,market=pick['market'],model_version=snapshot.model_version,probability=pick.get('probability'),outcome=outcome))
    return dict(build_trends(rows,as_of,truncated=truncated,duplicate_picks_excluded=duplicates),checked_at=utcnow())
