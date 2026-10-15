"""Bounded repair of final data for immutable saved prematch predictions.

The scraper's existing advisory lock serializes this queue. A claim is committed
before HTTP, so crashes/timeouts consume an attempt rather than restarting a loop.
"""
import asyncio
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import aliased
from src.models import Match, League, Team, MatchStatistics, PredictionSnapshot, CollectorRetry, utcnow
from src.analytics.performance import settle
from src.scrapers.goaloo.live import fetch_live
from src.scrapers.goaloo.statistics import parse_statistics
from src.scrapers.goaloo.client import SourceError
from .storage import store_live, store_statistics
from .worker import aware

MAX_ATTEMPTS=6
SCAN_BATCH=64
LOOKBACK_DAYS=30
DELAYS=(300,900,3600,21600,86400,86400)


async def recheck(updater, client):
    now=utcnow()
    home,away=aliased(Team),aliased(Team)
    async with updater.db.session() as session:
        rows=(await session.execute(select(Match,PredictionSnapshot,MatchStatistics,League.external_id,home.external_id,away.external_id)
            .join(PredictionSnapshot,PredictionSnapshot.match_id==Match.id).join(League)
            .join(home,home.id==Match.home_team_id).join(away,away.id==Match.away_team_id)
            .outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id)
            .where(League.enabled.is_(True),Match.id>getattr(updater,'settlement_cursor',0),
                Match.kickoff_at<=now-timedelta(hours=3),Match.kickoff_at>=now-timedelta(days=LOOKBACK_DAYS))
            .order_by(Match.id).limit(SCAN_BATCH))).all()
        if not rows:updater.settlement_cursor=0;return
        for match,snapshot,stats,league,home_id,away_id in rows:
            updater.settlement_cursor=match.id
            if aware(snapshot.captured_at)>=aware(match.kickoff_at):continue
            if not any(settle(match,stats,p,now)[0] in {'awaiting_data','pending'} for p in snapshot.picks):continue
            key=f'settlement:{match.external_match_id}'
            retry=await session.get(CollectorRetry,key)
            if retry and (retry.failures>=MAX_ATTEMPTS or aware(retry.next_attempt_at)>now):continue
            if retry is None:
                retry=CollectorRetry(key=key,failures=0,next_attempt_at=now,reason='final_data');session.add(retry)
            retry.failures+=1
            retry.next_attempt_at=now+timedelta(seconds=DELAYS[retry.failures-1])
            retry.reason='final_data'
            audit=dict(checked_at=now.isoformat(),attempts=retry.failures,state='exhausted' if retry.failures>=MAX_ATTEMPTS else 'waiting',
                       next_check_at=None if retry.failures>=MAX_ATTEMPTS else retry.next_attempt_at.isoformat(),source='goaloo')
            match.raw={**(match.raw or {}),'_settlement_check':audit}
            match_id,external=match.id,match.external_match_id
            await session.commit()
            break
        else:return
    # No connection or transaction is held while waiting on the source.
    observation=source=None
    error=None
    try:
        async with asyncio.timeout(8):
            observation,source=await fetch_live(client,external,league,home_id,away_id)
    except asyncio.CancelledError:raise
    except Exception as exc:error=type(exc).__name__
    async with updater.db.session() as session:
        current=await session.get(Match,match_id)
        if not await session.scalar(select(League.enabled).where(League.id==current.league_id)):return
        if observation:
            await store_live(session,current,observation,utcnow())
            try:await store_statistics(session,current,parse_statistics(source,external,league))
            except (ValueError,KeyError,SourceError) as exc:error=type(exc).__name__
        stats=await session.get(MatchStatistics,match_id)
        snapshot=await session.get(PredictionSnapshot,match_id)
        complete=all(settle(current,stats,p,now)[0] not in {'pending','awaiting_data'} for p in snapshot.picks)
        audit={**audit,'state':'complete' if complete else 'exhausted' if audit['attempts']>=MAX_ATTEMPTS else 'waiting',
               'error':error,'next_check_at':None if complete or audit['attempts']>=MAX_ATTEMPTS else audit['next_check_at']}
        current.raw={**(current.raw or {}),'_settlement_check':audit}
        retry=await session.get(CollectorRetry,key)
        retry.reason='complete' if complete else error or 'missing_final_data'
        await session.commit()
