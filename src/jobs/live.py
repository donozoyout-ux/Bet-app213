"""Small priority cycles, independent of the historical job queue.

Called only by the advisory-lock-owning scraper, between durable items. Each
cycle is bounded; old jobs retain their running state and completed checkpoints.
"""
import asyncio
import logging
import time
from datetime import timedelta, timezone
from sqlalchemy import select, or_, case
from sqlalchemy.orm import aliased

from src.models import Match, League, Team, MatchStatistics, utcnow
from src.match_views import LIVE_STATUSES, day_bounds, DISPLAY_TIMEZONE
from zoneinfo import ZoneInfo
from src.scrapers.goaloo.live import fetch_live
from src.scrapers.goaloo.statistics import parse_statistics, fetch_statistics
from src.scrapers.goaloo.client import SourceError
from .storage import store_live, store_statistics
from .resources import memory_constrained

log=logging.getLogger('scraper.live')


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class PriorityUpdater:
    def __init__(self, db):
        self.db=db
        self.next_tick=0
        self.next_recent=0
        self.league_cursor=0

    async def tick(self, client):
        if time.monotonic()<self.next_tick or memory_constrained():return
        self.next_tick=time.monotonic()+5
        try:
            async with asyncio.timeout(25):
                await self.live_cycle(client)
            if time.monotonic()>=self.next_recent:
                self.next_recent=time.monotonic()+15
                async with asyncio.timeout(10):
                    await self.recent_statistics(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A source outage must not turn an unrelated historical job into failed.
            log.warning('[LIVE] priority cycle deferred error=%s',type(exc).__name__)

    async def live_cycle(self, client):
        now=utcnow()
        start,end=day_bounds(now.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).date(),DISPLAY_TIMEZONE)
        home,away=aliased(Team),aliased(Team)
        urgent=or_(Match.status.in_(LIVE_STATUSES),Match.kickoff_at<=now+timedelta(minutes=30))
        async with self.db.session() as session:
            rows=(await session.execute(select(Match,League.external_id,home.external_id,away.external_id)
                .join(League).join(home,home.id==Match.home_team_id).join(away,away.id==Match.away_team_id)
                .where(League.enabled.is_(True),
                    or_(Match.status.in_(LIVE_STATUSES),
                        (Match.status.in_(['scheduled','pending','interrupted'])) &
                        (Match.kickoff_at>=start-timedelta(days=1)) & (Match.kickoff_at<end)),
                    or_(Match.live_checked_at.is_(None),
                        urgent & (Match.live_checked_at<=now-timedelta(seconds=60)),
                        ~urgent & (Match.live_checked_at<=now-timedelta(minutes=10))))
                .order_by(case((urgent,0),else_=1),Match.live_checked_at.asc().nullsfirst(),Match.kickoff_at,Match.id).limit(32))).all()
        deadline=time.monotonic()+20
        for match,league,home_id,away_id in rows:
            if time.monotonic()>=deadline:break
            try:
                async with asyncio.timeout(8):
                    observation,source=await fetch_live(client,match.external_match_id,league,home_id,away_id)
                async with self.db.session() as session:
                    current=await session.get(Match,match.id)
                    if not await session.scalar(select(League.enabled).where(League.id==current.league_id)):continue
                    accepted=await store_live(session,current,observation,utcnow())
                    await session.commit()
                if accepted:
                    # Score survives a changed/missing statistics table.
                    try:
                        stats=parse_statistics(source,match.external_match_id,league)
                        async with self.db.session() as session:
                            await store_statistics(session,await session.get(Match,match.id),stats)
                            await session.commit()
                    except (SourceError,ValueError) as exc:
                        log.warning('[LIVE] statistics deferred match_id=%s error=%s',match.external_match_id,type(exc).__name__)
                log.info('[LIVE] match_id=%s status=%s score=%s-%s minute=%s',
                    match.external_match_id,observation['status'],observation['ft_home'],observation['ft_away'],observation['live_minute'])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('[LIVE] refresh failed match_id=%s error=%s',match.external_match_id,type(exc).__name__)
                async with self.db.session() as session:
                    current=await session.get(Match,match.id)
                    current.live_checked_at=utcnow()
                    await session.commit()

    async def recent_statistics(self,client):
        """One recent fixture per league in round-robin order, ahead of old history."""
        now=utcnow()
        async with self.db.session() as session:
            leagues=(await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.priority,League.id))).all()
        if not leagues:return
        for offset in range(len(leagues)):
            index=(self.league_cursor+offset)%len(leagues)
            league=leagues[index]
            async with self.db.session() as session:
                # A final partial observation is retried after six hours, never as zero.
                full=(MatchStatistics.is_final.is_(True) & MatchStatistics.home_corners.is_not(None) &
                    MatchStatistics.away_corners.is_not(None) & MatchStatistics.home_yellow_cards.is_not(None) &
                    MatchStatistics.away_yellow_cards.is_not(None) & MatchStatistics.home_red_cards.is_not(None) &
                    MatchStatistics.away_red_cards.is_not(None))
                observed=select(MatchStatistics.match_id).where(MatchStatistics.match_id==Match.id,
                    or_(full,(MatchStatistics.is_final.is_(True) & (MatchStatistics.updated_at>now-timedelta(hours=6))))).exists()
                match=await session.scalar(select(Match).where(Match.league_id==league.id,Match.status=='finished',
                    Match.kickoff_at>=now-timedelta(days=90),~observed).order_by(Match.kickoff_at.desc(),Match.id.desc()).limit(1))
            if not match:continue
            self.league_cursor=(index+1)%len(leagues)
            try:
                async with asyncio.timeout(8):
                    stats=await fetch_statistics(client,match.external_match_id,league.external_id)
                async with self.db.session() as session:
                    if not await session.scalar(select(League.enabled).where(League.id==league.id)):return
                    await store_statistics(session,await session.get(Match,match.id),stats)
                    await session.commit()
                log.info('[STATS] recent league=%s match_id=%s',league.external_id,match.external_match_id)
            except (SourceError,TimeoutError) as exc:
                log.warning('[STATS] recent deferred league=%s error=%s',league.external_id,type(exc).__name__)
            return


async def priority_cycle(db,client):
    import os
    from src.config import settings
    if settings.app_env!='production' and os.getenv('BETAPP_PROCESS_ROLE')!='scraper':return
    if not hasattr(db,'priority_updater'):db.priority_updater=PriorityUpdater(db)
    await db.priority_updater.tick(client)
