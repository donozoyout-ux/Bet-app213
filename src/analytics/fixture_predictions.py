"""Bounded daily fixture catalog. Reads original snapshots, never generates picks."""
import math
from datetime import timedelta
from sqlalchemy import select, func, case, and_, text
from sqlalchemy.orm import aliased, defer
from src.models import League, Team, Match, MatchStatistics, PredictionSnapshot as P, PredictionSettlement as S
from src.match_views import LIVE_STATUSES, DISPLAY_TIMEZONE, day_bounds
from src.jobs.worker import aware
from src.analytics.performance import display_result

LIVE_FRESH_SECONDS=180
MAX_PICKS=100


def score_state(match, now):
    observed=aware(match.status_observed_at)
    valid_time=observed is not None and observed<=now
    valid_score=all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in (match.ft_home,match.ft_away))
    result=dict(home=None,away=None,observed_at=observed,state='unknown',fresh_until=None)
    if match.status in LIVE_STATUSES and valid_time and valid_score:
        until=observed+timedelta(seconds=LIVE_FRESH_SECONDS)
        result.update(home=match.ft_home,away=match.ft_away,state='fresh' if until>now else 'stale',fresh_until=until)
    elif match.status=='finished' and valid_score:
        result.update(home=match.ft_home,away=match.ft_away,state='recorded_final' if valid_time else 'unknown_time')
    return result


def original_picks(snapshot, match, stats, decisions):
    if snapshot is None:return None
    picks=[];seen=set()
    for index,pick in enumerate(snapshot.picks[:MAX_PICKS]):
        identity=(pick.get('market'),pick.get('selection'))
        if identity in seen:continue
        seen.add(identity);saved=decisions.get(index)
        outcome='pending' if match.status in (*LIVE_STATUSES,'scheduled','postponed') else 'awaiting_data'
        reason='settlement_not_recorded';actual=None;verified=False;evidence=None
        if saved:
            outcome,actual,reason=display_result(snapshot,match,stats,pick,saved)
            verified=reason=='recorded_verified' and outcome in {'won','lost','void'}
            if reason!='invalid_recorded_evidence':
                proof=saved.evidence
                evidence={key:proof.get(key) for key in ('status','ft_home','ft_away','ht_home','ht_away','is_final','source','score_observed_at','statistics_updated_at')}
                evidence['statistics']={key:proof.get('statistics',{}).get(key) for key in (
                    'home_corners','away_corners','home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards')}
        probability=pick.get('probability')
        if isinstance(probability,bool) or not isinstance(probability,(float,int)) or not math.isfinite(probability) or not 0<=probability<=1:probability=None
        picks.append(dict(index=index,id=pick.get('id'),market=pick.get('market'),selection=pick.get('selection'),
            label=pick.get('label') or f"{pick.get('market','')} · {pick.get('selection','')}",probability=probability,
            outcome=outcome,verified=verified,reason=reason,actual=actual,
            settlement=dict(id=saved.id,revision=saved.revision,recorded_at=aware(saved.recorded_at),
                recorded_outcome=saved.outcome,evidence=evidence) if saved else None))
    return dict(captured_at=aware(snapshot.captured_at),model_version=snapshot.model_version,
        picks=picks,truncated=len(snapshot.picks)>MAX_PICKS)


async def fixture_page(session, day, now, *, league=None, status='all', limit=25, offset=0):
    # Consistent counts/page/evidence if the collector commits a live transition.
    if session.bind.dialect.name=='postgresql':
        await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
    start,end=day_bounds(day)
    eligible=and_(P.match_id==Match.id,P.captured_at<Match.kickoff_at)
    filters=[League.enabled.is_(True),Match.kickoff_at>=start,Match.kickoff_at<end]
    if league:filters.append(League.id==league)
    def count(condition):return func.coalesce(func.sum(case((condition,1),else_=0)),0)
    summary=dict((await session.execute(select(func.count(Match.id).label('total'),
        count(Match.status=='finished').label('finished'),count(Match.status.in_(LIVE_STATUSES)).label('live'),
        count(Match.status=='scheduled').label('upcoming'),
        count(and_(P.match_id.is_not(None),func.json_array_length(P.picks)>0)).label('with_snapshot'))
        .select_from(Match).join(League,League.id==Match.league_id).outerjoin(P,eligible).where(*filters))).mappings().one())
    summary['other']=summary['total']-summary['finished']-summary['live']-summary['upcoming']
    home,away=aliased(Team),aliased(Team)
    query=(select(Match,P,MatchStatistics,League.name,League.source,home.name,away.name)
        .join(League,League.id==Match.league_id).join(home,home.id==Match.home_team_id).join(away,away.id==Match.away_team_id)
        .outerjoin(P,eligible).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id)
        .options(defer(Match.raw),defer(MatchStatistics.raw)).where(*filters))
    if status=='live':query=query.where(Match.status.in_(LIVE_STATUSES))
    elif status=='finished':query=query.where(Match.status=='finished')
    elif status=='upcoming':query=query.where(Match.status=='scheduled')
    rows=(await session.execute(query.order_by(Match.kickoff_at,Match.id).limit(limit).offset(offset))).all()
    ids=[match.id for match,snapshot,*_ in rows if snapshot is not None]
    decisions={}
    if ids:
        latest=select(S.id.label('id'),func.row_number().over(partition_by=(S.match_id,S.pick_index),
            order_by=(S.revision.desc(),S.id.desc())).label('position')).where(S.match_id.in_(ids)).subquery()
        for saved in await session.scalars(select(S).join(latest,latest.c.id==S.id).where(latest.c.position==1)):
            decisions.setdefault(saved.match_id,{})[saved.pick_index]=saved
    items=[];verified=0
    for match,snapshot,stats,league_name,source,home_name,away_name in rows:
        original=original_picks(snapshot,match,stats,decisions.get(match.id,{}))
        verified+=sum(p['verified'] for p in (original or {}).get('picks',[]))
        items.append(dict(id=match.id,league_id=match.league_id,league=league_name,source=source,
            home_team=home_name,away_team=away_name,kickoff_at=aware(match.kickoff_at),status=match.status,
            live_minute=match.live_minute,status_observed_at=aware(match.status_observed_at),
            checked_at=aware(match.live_checked_at),updated_at=aware(match.updated_at),score=score_state(match,now),snapshot=original))
    return dict(date=day.isoformat(),timezone=DISPLAY_TIMEZONE,as_of=now,summary=summary,
        page_summary=dict(verified_outcomes=verified,scope='displayed_page'),
        items=items,total=summary['total'] if status=='all' else summary[status],limit=limit,offset=offset,
        cache_seconds=15,poll_seconds=60,live_fresh_seconds=LIVE_FRESH_SECONDS)
