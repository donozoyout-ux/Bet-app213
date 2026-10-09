"""One durable worker per database, enforced using a PostgreSQL advisory lock."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, text, func, delete, case, or_
from src.config import settings
from src.match_views import LIVE_STATUSES
from src.db import database
from src.models import League, Season, Match, Bookmaker, ScraperJob, JobItem, ScraperIssue, utcnow
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.seasons import discover_seasons, season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.details import fetch_match_details
from src.scrapers.goaloo.odds import fetch_odds, complete_odds, fetch_history
from .storage import store_match, store_odds, store_history, get_or_create
from .storage import store_statistics
from src.models import MatchStatistics
from src.scrapers.goaloo.statistics import fetch_statistics
from .resources import wait_for_capacity
from .live import priority_cycle

log = logging.getLogger('scraper')
LOCK_ID = 213002


def missing_statistics_query(league_id, start_year=2024):
    """Retry old incomplete observations, never reinterpret missing values as zero."""
    complete = (MatchStatistics.is_final.is_(True) &
        MatchStatistics.home_corners.is_not(None) & MatchStatistics.away_corners.is_not(None) &
        MatchStatistics.home_yellow_cards.is_not(None) & MatchStatistics.away_yellow_cards.is_not(None) &
        MatchStatistics.home_red_cards.is_not(None) & MatchStatistics.away_red_cards.is_not(None))
    observed = select(MatchStatistics.match_id).where(
        MatchStatistics.match_id == Match.id,
        or_(MatchStatistics.updated_at > utcnow()-timedelta(days=7), complete)).exists()
    return select(Match).where(Match.league_id == league_id, Match.status == 'finished',
        Match.kickoff_at >= datetime(start_year, 1, 1, tzinfo=timezone.utc), ~observed)


async def league_enabled(session, job):
    return bool(await session.scalar(select(League.enabled).where(League.id == job.league_id)))


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def is_final(match):
    return match.status == 'finished' or (match.status in LIVE_STATUSES and match.kickoff_at is not None and aware(match.kickoff_at) <= utcnow())


async def enqueue(kind, league_external_id=36, start_year=2024, match_id=None, resume_id=None):
    async with database.session() as session:
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        league = await session.scalar(select(League).where(League.external_id == league_external_id))
        if league is None or not league.enabled:
            raise ValueError('League is not configured')
        if resume_id:
            job = await session.get(ScraperJob, resume_id)
            if not job or job.kind != kind or job.league_id != league.id:
                raise ValueError('Resume job does not match request')
            if job.status in {'queued', 'running'}:
                return job
            duplicate = await session.scalar(select(ScraperJob.id).where(
                ScraperJob.league_id == league.id, ScraperJob.kind == kind,
                ScraperJob.match_id == job.match_id, ScraperJob.id != job.id,
                ScraperJob.status.in_(['queued', 'running'])))
            if duplicate:
                raise ValueError(f'An equivalent job is already active: {duplicate}')
            job.status, job.finished_at, job.last_error = 'queued', None, None
            if await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id)):
                job.discovery_complete = False
            items = (await session.scalars(select(JobItem).where(JobItem.job_id == job.id, JobItem.status != 'completed'))).all()
            for item in items:
                item.status, item.error = 'queued', None
            job.failed_matches = 0
        else:
            query=select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.status.in_(['queued', 'running']))
            if kind=='update':query=query.where(ScraperJob.kind=='update')
            active = await session.scalar(query)
            if active:
                raise ValueError(f'League already has an active job: {active.id}')
            job = ScraperJob(kind=kind, league_id=league.id, start_year=start_year, match_id=match_id, priority=0 if kind == 'update' else league.priority)
            session.add(job)
        await session.commit()
        return job


async def enqueue_initial_backfill(db=None, enabled=None):
    """Enqueue once on empty PostgreSQL, atomically under the enqueue lock."""
    db = db or database
    enabled = settings.auto_backfill_on_empty if enabled is None else enabled
    if not enabled or not db.ready or db.engine.dialect.name != 'postgresql':
        return None
    async with db.session() as session:
        await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        return await initial_backfill_job(session)


async def initial_backfill_job(session):
    """Caller owns the enqueue transaction lock; storage rules are independently testable."""
    league = await session.scalar(select(League).where(League.external_id == 36))
    if league is None:
        return None
    matches = await session.scalar(select(func.count(Match.id)).where(Match.league_id == league.id))
    if matches:
        return None
    previous = await session.scalar(select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.kind == 'backfill').order_by(ScraperJob.id.desc()))
    active = await session.scalar(select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.status.in_(['queued','running'])))
    if previous or active:
        return previous or active
    job = ScraperJob(kind='backfill', league_id=league.id, start_year=2024, priority=league.priority)
    session.add(job)
    await session.commit()
    log.info('[BACKFILL] initial backfill queued job_id=%s league=36 start_year=2024', job.id)
    return job


async def enqueue_catalog_backfills(db=None, enabled=None):
    """One durable historical job per verified, enabled competition; execution is serial."""
    db = db or database
    enabled = settings.auto_backfill_on_empty if enabled is None else enabled
    if not enabled or not db.ready or db.engine.dialect.name != 'postgresql':
        return []
    async with db.session() as session:
        await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        jobs = await queue_missing_competitions(session)
        await session.commit()
        return jobs


async def queue_missing_competitions(session):
    leagues = (await session.scalars(select(League).where(League.enabled.is_(True), League.verified_at.is_not(None)).order_by(League.priority, League.id))).all()
    jobs = []
    for league in leagues:
        previous = await session.scalar(select(ScraperJob.id).where(ScraperJob.league_id == league.id, ScraperJob.kind == 'backfill'))
        matches = await session.scalar(select(func.count(Match.id)).where(Match.league_id == league.id))
        active = await session.scalar(select(ScraperJob.id).where(ScraperJob.league_id == league.id, ScraperJob.status.in_(['queued','running'])))
        if previous or matches or active:
            continue
        job = ScraperJob(kind='backfill', league_id=league.id, start_year=league.backfill_from_year, priority=league.priority)
        session.add(job)
        jobs.append(job)
    await session.flush()
    return jobs


async def enqueue_all_updates():
    async with database.session() as session:
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        result = []
        for league in (await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.priority))).all():
            active = await session.scalar(select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.kind=='update', ScraperJob.status.in_(['queued','running'])))
            if active:
                result.append(active)
            else:
                job = ScraperJob(kind='update', league_id=league.id, start_year=league.backfill_from_year, priority=0)
                session.add(job)
                result.append(job)
        await session.commit()
        return result

async def discover(job_id, client):
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        league = await session.get(League, job.league_id)
        kind, start_year, league_id, external_id = job.kind, job.start_year, league.id, league.external_id
        competition_type, schedule_format = league.competition_type, league.schedule_format
        if job.discovery_complete:
            return
        if kind == 'stats_backfill':
            query=select(Match).where(Match.league_id==league_id)
            if job.match_id:query=query.where(Match.id==job.match_id)
            else:query=missing_statistics_query(league_id,start_year)
            for match in (await session.scalars(query.order_by(Match.kickoff_at.desc(),Match.id.desc()))).all():
                await get_or_create(session,JobItem,{'job_id':job.id,'match_id':match.id})
            await session.flush()
            job.total_matches=await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id==job.id))
            job.discovery_complete=True
            await session.commit()
            return
        if kind == 'match':
            match = await session.get(Match, job.match_id)
            season = await session.get(Season, match.season_id)
            await session.commit()
            data = await fetch_match_details(client, match.external_match_id, external_id, season.season_name, schedule_format='cup') if schedule_format == 'cup' else await fetch_match_details(client, match.external_match_id, external_id, season.season_name)
            await store_match(session, league_id, season.season_name, data)
            await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': match.id})
            job.total_matches, job.discovery_complete = 1, True
            await session.commit()
            return
    seasons = await discover_seasons(client, external_id, start_year, overlap=True) if competition_type == 'national' else await discover_seasons(client, external_id, start_year)
    if not seasons:
        raise SourceError('No seasons available from the requested year')
    if kind == 'update':
        # Include the previous season for summer transitions and late corrections.
        seasons = seasons[-2:]
    now = utcnow()
    for season_name in seasons:
        payload = await season_data(client, external_id, season_name, schedule_format='cup') if schedule_format == 'cup' else await season_data(client, external_id, season_name)
        if payload.get('LeagueInfo'):
            from src.scrapers.goaloo.competitions import verified_snapshot, Competition, validate_identity
            source = next((record for record in verified_snapshot() if record['external_id'] == external_id), None)
            if source:
                validate_identity(Competition(**source), payload, season_name)
        for round_number in discover_rounds(payload):
            await priority_cycle(database,client)
            log.info('[GOALOO] league=%s season=%s round=%s', external_id, season_name, round_number)
            async with database.session() as session:
                job = await session.get(ScraperJob, job_id)
                if not await league_enabled(session, job):
                    job.status = 'queued'
                    await session.commit()
                    return
                job.current_season, job.current_round = season_name, round_number
                # A transaction commits each round plus job items; rediscovery is idempotent.
                await session.execute(delete(ScraperIssue).where(ScraperIssue.job_id == job.id, ScraperIssue.season == season_name, ScraperIssue.round == round_number))
                errors = []
                for data in parse_matches(payload, round_number, errors):
                    if data['external_league_id'] != external_id:
                        raise SourceError('League ID does not match request')
                    if competition_type == 'national' and (data['kickoff_at'] is None or data['kickoff_at'].year < start_year):
                        continue
                    match = await store_match(session, league_id, season_name, data)
                    kickoff = aware(match.kickoff_at)
                    recent = kickoff is not None and now - timedelta(days=7) <= kickoff <= now + timedelta(days=7)
                    needs_odds = kind == 'backfill' and not (match.status == 'finished' and match.odds_complete)
                    needs_odds = needs_odds or (kind == 'update' and recent)
                    if needs_odds and match.status not in {'cancelled', 'postponed', 'abandoned', 'pending'}:
                        await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': match.id})
                for error in errors:
                    session.add(ScraperIssue(job_id=job.id, season=season_name, round=round_number, **error))
                    job.last_error = error['error']
                    log.warning('[MATCH] parse failed %s', error['error'])
                await session.flush()
                issues = await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id))
                job.total_matches = issues + await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id))
                await update_counts(session, job)
                await session.commit()
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        job.discovery_complete = True
        await session.commit()


async def run_job(job_id):
    log.info('[WORKER] job started id=%s', job_id)
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        if not await league_enabled(session, job):return
        job.status, job.started_at, job.finished_at = 'running', job.started_at or utcnow(), None
        await session.commit()
    try:
        async with GoalooClient() as client:
            await priority_cycle(database,client)
            await discover(job_id, client)
            async with database.session() as session:
                item_ids = list(await session.scalars(select(JobItem.id).where(JobItem.job_id == job_id, JobItem.status.in_(['queued', 'running'])).order_by(JobItem.id)))
            for item_id in item_ids:
                await wait_for_capacity()
                await priority_cycle(database,client)
                async with database.session() as session:
                    job = await session.get(ScraperJob, job_id)
                    if job.kind!='update' and await session.scalar(select(ScraperJob.id).join(League).where(
                        League.enabled.is_(True),ScraperJob.kind=='update',ScraperJob.status=='queued').limit(1)):
                        return  # Keep running checkpoint; priority update runs next.
                    if not await league_enabled(session, job):
                        job.status = 'queued'
                        await session.commit()
                        return
                await process_item(job_id, item_id, client)
        async with database.session() as session:
            job = await session.get(ScraperJob, job_id)
            if not await league_enabled(session, job):
                job.status = 'queued'
                await session.commit()
                return
            await update_counts(session, job)
            job.status = 'partial' if job.failed_matches else 'completed'
            job.finished_at = utcnow()
            await session.commit()
            log.info('[WORKER] job completed id=%s status=%s processed=%s failed=%s', job.id, job.status, job.processed_matches, job.failed_matches)
        try:
            from src.analytics.performance import capture_upcoming
            async with database.session() as session:await capture_upcoming(session,job.league_id)
        except Exception as exc:
            log.warning('[PREDICTIONS] capture deferred: %s',type(exc).__name__)
    except asyncio.CancelledError:
        # Job stays running; the next lock-owning worker resumes persisted items.
        raise
    except Exception as exc:
        log.exception('[JOB] failed job_id=%s', job_id)
        async with database.session() as session:
            job = await session.get(ScraperJob, job_id)
            job.status, job.finished_at = 'failed', utcnow()
            job.last_error = str(exc)[:500] if isinstance(exc, SourceError) else f'Job failed: {type(exc).__name__}'
            await session.commit()


async def process_item(job_id, item_id, client):
    external_id = None
    try:
        async with database.session() as session:
            item = await session.get(JobItem, item_id)
            match = await session.get(Match, item.match_id)
            job = await session.get(ScraperJob, job_id)
            season = await session.get(Season, match.season_id)
            external_id, final = match.external_match_id, is_final(match)
            job.current_season, job.current_round = season.season_name, match.round
            item.status = 'running'
            await session.commit()
            log.info('[MATCH] match_id=%s season=%s round=%s', external_id, season.season_name, match.round)
            league=await session.get(League,match.league_id)
            # expire_on_commit=False keeps the loaded metadata usable while releasing
            # the connection before potentially slow HTTP retries.
            await session.commit()
            if job.kind=='stats_backfill':
                data=await fetch_statistics(client,external_id,league.external_id)
                await store_statistics(session,match,data)
                ok=True  # Valid unavailable statistics are persisted, never fabricated.
            else:
                errors=[]
                try:
                    odds = await fetch_odds(client, external_id, final)
                    await store_odds(session, match, odds, final)
                    await session.commit()  # A later source failure cannot roll back valid odds.
                    ok = complete_odds(odds, final)
                    if not ok:errors.append('Incomplete bookmaker markets')
                except SourceError as exc:
                    ok=False
                    errors.append(str(exc))
                if match.status=='finished' or match.status in LIVE_STATUSES:
                    try:
                        data=await fetch_statistics(client,external_id,league.external_id)
                        await store_statistics(session,match,data)
                        await session.commit()
                    except SourceError:
                        log.warning('[STATISTICS] source unavailable match_id=%s; dedicated stats job will retry',external_id)
                if settings.save_snapshots:
                    bookmakers = (await session.scalars(select(Bookmaker))).all()
                    await session.commit()
                    for bookmaker in bookmakers:
                        try:
                            history = await fetch_history(client, external_id, bookmaker.external_id)
                            await store_history(session, match.id, bookmaker.id, history)
                            await session.commit()
                        except SourceError as exc:
                            ok=False
                            errors.append('History: '+str(exc))
            item.status = 'completed' if ok else 'failed'
            item.error = None if ok else ('; '.join(errors)+f' match_id={external_id}')[:500]
            await session.flush()
            await update_counts(session, job)
            if not ok:
                job.last_error = item.error
            await session.commit()
            log.info('[JOB] job_id=%s processed=%s/%s failed=%s', job.id, job.processed_matches, job.total_matches, job.failed_matches)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception('[MATCH] failed match_id=%s job_id=%s', external_id, job_id)
        async with database.session() as session:
            item = await session.get(JobItem, item_id)
            job = await session.get(ScraperJob, job_id)
            item.status = 'failed'
            item.error = f'match_id={external_id}: ' + (str(exc)[:400] if isinstance(exc, SourceError) else type(exc).__name__)
            job.last_error = item.error
            await session.flush()
            await update_counts(session, job)
            await session.commit()


async def update_counts(session, job):
    counts = dict((await session.execute(select(JobItem.status, func.count(JobItem.id)).where(JobItem.job_id == job.id).group_by(JobItem.status))).all())
    job.processed_matches = counts.get('completed', 0)
    issues = await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id))
    job.failed_matches = counts.get('failed', 0) + issues


async def work(once=False):
    initialization_checked = False
    stats_queue_checked = float('-inf')
    while True:
        try:
            await wait_for_capacity()
            if not database.ready:
                await database.initialize()
            if not initialization_checked:
                await enqueue_initial_backfill(database)
                await enqueue_catalog_backfills(database)
                initialization_checked = True
            if settings.auto_backfill_on_empty and database.engine.dialect.name == 'postgresql' and time.monotonic()-stats_queue_checked >= 60:
                await enqueue_stats_rollout(automatic=True)
                stats_queue_checked=time.monotonic()
            async with database.engine.connect() as lock:
                postgres = lock.dialect.name == 'postgresql'
                acquired = not postgres or await lock.scalar(text('SELECT pg_try_advisory_lock(:id)'), {'id': LOCK_ID})
                await lock.commit()
                if not acquired:
                    if once:
                        return
                    await asyncio.sleep(5)
                    continue
                try:
                    async with GoalooClient() as client:
                        await priority_cycle(database,client)
                    async with database.session() as session:
                        job_id = await session.scalar(select(ScraperJob.id).join(League).where(League.enabled.is_(True), ScraperJob.status.in_(['running', 'queued'])).order_by(case((ScraperJob.kind == 'update', 0), else_=1),case((ScraperJob.status == 'running', 0), else_=1), ScraperJob.priority, ScraperJob.id).limit(1))
                    if job_id:
                        await run_job(job_id)
                finally:
                    if postgres:
                        await lock.execute(text('SELECT pg_advisory_unlock(:id)'), {'id': LOCK_ID})
                        await lock.commit()
            if once:
                return
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            raise
        except Exception:
            database.ready = False
            log.exception('[WORKER] database unavailable; retrying')
            if once:
                raise
            await asyncio.sleep(10)


async def enqueue_stats_rollout(automatic=False):
    """One active stats job per enabled league, independent of its odds backfill.

    Explicit calls resume failures immediately. Automatic reconciliation has a
    one-day failure cooldown; partial source observations have a seven-day TTL.
    Completed items survive resumes. Newly discovered matches get a new job.
    """
    from src.scrapers.goaloo.competitions import PRODUCTION_LEAGUE_IDS
    async with database.session() as session:
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        jobs=[]
        for priority, external_id in enumerate(PRODUCTION_LEAGUE_IDS, 1):
            league=await session.scalar(select(League).where(League.enabled.is_(True),League.external_id==external_id))
            if not league:continue
            previous=await session.scalar(select(ScraperJob).where(ScraperJob.league_id==league.id,
                ScraperJob.kind=='stats_backfill',ScraperJob.match_id.is_(None)).order_by(ScraperJob.id.desc()).limit(1))
            if previous and previous.status in {'queued','running'}:
                jobs.append(previous)
                continue
            if previous and previous.status in {'partial','failed'}:
                if automatic and previous.finished_at and aware(previous.finished_at)>utcnow()-timedelta(days=1):
                    jobs.append(previous)
                    continue
                for item in (await session.scalars(select(JobItem).where(JobItem.job_id==previous.id,JobItem.status!='completed'))).all():
                    item.status,item.error='queued',None
                previous.status,previous.finished_at,previous.last_error='queued',None,None
                previous.failed_matches=0
                jobs.append(previous)
                continue
            if previous and not await session.scalar(missing_statistics_query(league.id).with_only_columns(Match.id).limit(1)):
                jobs.append(previous)
                continue
            # A stats job may wait behind an odds job. The single global worker
            # lock serializes both; odds jobs never stand in for statistics jobs.
            job=ScraperJob(kind='stats_backfill',league_id=league.id,start_year=2024,priority=priority)
            session.add(job)
            jobs.append(job)
        await session.commit()
        return jobs
