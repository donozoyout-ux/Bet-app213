"""One durable worker per database, enforced using a PostgreSQL advisory lock."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
import httpx
from sqlalchemy import select, text, func, delete, case, or_, and_
from src.config import settings

from src.match_views import LIVE_STATUSES
from src.db import database
from src.models import League, Season, Match, Bookmaker, ScraperJob, JobItem, ScraperIssue, MarketOdds, utcnow
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.seasons import discover_seasons, season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.details import fetch_match_details
from src.scrapers.goaloo.odds import fetch_odds, complete_odds, fetch_history, fetch_halftime_odds
from src.scrapers.goaloo.corner_odds import fetch_corner_odds
from .storage import store_match, store_odds, store_market_odds, store_history, get_or_create
from .storage import store_statistics
from src.models import MatchStatistics
from src.scrapers.goaloo.statistics import fetch_statistics
from .resources import wait_for_capacity
from .live import priority_cycle
from .europe_capacity import HistoryCapacityError, guard_history_write, historical

log = logging.getLogger('scraper')
LOCK_ID = 213002
HISTORY_SLICE_SIZE = 25
DURABLE_KINDS = ('backfill','update','stats_backfill','market_backfill','market_odds','match','domestic_history')


async def pause_capacity(job_id, error):
    """Preserve item state, counters and discovery checkpoints for explicit resume."""
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        if job.status not in {'paused_disabled', 'manual_paused'}:
            job.status, job.last_error = 'paused_capacity', str(error)[:500]
            await session.commit()


async def reconcile_owned_queue(session):
    """Call only while owning LOCK_ID: no other durable worker can be active.

    Running means interrupted, not expired by an arbitrary wall clock. Preserve
    completed/failed items, discovery, issue evidence, counters and started_at.
    Disabled work is parked visibly and can resume if the league is re-enabled.
    Manual imports use their own lock and are never adopted by this worker.
    """
    jobs = (await session.scalars(select(ScraperJob).where(
        ScraperJob.status.in_(['running', 'queued', 'paused_disabled']),
        ScraperJob.kind.in_(DURABLE_KINDS)
    ))).all()
    for job in jobs:
        enabled = await league_enabled(session, job)
        target = 'queued' if enabled else 'paused_disabled'
        if job.status == 'running':
            items = (await session.scalars(select(JobItem).where(
                JobItem.job_id == job.id, JobItem.status == 'running'))).all()
            for item in items:
                item.status = 'queued'
            log.info('[QUEUE] recovered job=%s items=%s target=%s', job.id, len(items), target)
        job.status = target
    await session.commit()


def next_job_query():
    # Daily updates retain priority. History rotates after each bounded slice,
    # instead of preferring a running job forever. Never select manual imports.
    return select(ScraperJob.id).join(League).where(
        League.enabled.is_(True), ScraperJob.status.in_(['queued','running']),
        ScraperJob.kind.in_(DURABLE_KINDS)
    ).order_by(case((ScraperJob.kind == 'update', 0), else_=1),
        ScraperJob.checkpoint_at.asc().nulls_first(), ScraperJob.priority, ScraperJob.id).limit(1)


async def yield_job(job_id):
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        job.status = 'queued' if await league_enabled(session, job) else 'paused_disabled'
        job.checkpoint_at = utcnow()
        await session.commit()


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


def missing_market_odds_query(league_id, start_year=2024):
    """Find matches missing verified period-specific market odds (HT OU or Corner OU).

    Independent check: a match needs backfill if it lacks meaningful stored odds/lines
    for either first_half_goals or corners, unless both markets have verified lines
    or the match was checked within the controlled 7-day cooldown.
    """
    has_meaningful_ht = select(MarketOdds.match_id).where(
        MarketOdds.match_id == Match.id,
        MarketOdds.market == 'first_half_goals',
        MarketOdds.period == 'first_half',
        or_(
            MarketOdds.latest_line.is_not(None),
            MarketOdds.closing_line.is_not(None),
            MarketOdds.opening_line.is_not(None),
        ),
    ).exists()

    has_meaningful_corners = select(MarketOdds.match_id).where(
        MarketOdds.match_id == Match.id,
        MarketOdds.market == 'corners',
        MarketOdds.period == 'full_time',
        or_(
            MarketOdds.latest_line.is_not(None),
            MarketOdds.closing_line.is_not(None),
            MarketOdds.opening_line.is_not(None),
        ),
    ).exists()

    complete_odds = has_meaningful_ht & has_meaningful_corners

    recent_check = select(JobItem.match_id).join(
        ScraperJob, ScraperJob.id == JobItem.job_id
    ).where(
        JobItem.match_id == Match.id,
        ScraperJob.kind.in_(['market_backfill', 'market_odds']),
        JobItem.status.in_(['completed', 'no_odds']),
        or_(
            ScraperJob.finished_at > utcnow() - timedelta(days=7),
            ScraperJob.created_at > utcnow() - timedelta(days=7),
        ),
    ).exists()

    observed = complete_odds | recent_check
    return select(Match).where(
        Match.league_id == league_id,
        Match.status.notin_(['cancelled', 'postponed', 'abandoned', 'pending']),
        Match.kickoff_at >= datetime(start_year, 1, 1, tzinfo=timezone.utc),
        ~observed,
    )




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
            if job.status in {'paused_capacity','paused_disabled'}:
                job.status = 'queued'
                await session.commit()
                return job  # Explicit resume retains source errors and all items.
            job.status, job.finished_at, job.last_error = 'queued', None, None
            if await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id)):
                job.discovery_complete = False
            items = (await session.scalars(select(JobItem).where(JobItem.job_id == job.id, JobItem.status != 'completed'))).all()
            for item in items:
                item.status, item.error = 'queued', None
            job.failed_matches = 0
        else:
            query=select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.status.in_(['queued', 'running','paused_capacity','paused_disabled']))
            if kind in ('update', 'stats_backfill', 'market_backfill', 'market_odds'):
                query=query.where(ScraperJob.kind==kind)
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
        # European pilots require an explicit supervised historical job.
        if league.external_id in (315, 348):
            continue
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
            if league.external_id in (315,348) and not await session.scalar(select(Match.id).where(Match.league_id==league.id).limit(1)):
                continue  # Empty pilots require a supervised import first.
            active = await session.scalar(select(ScraperJob).where(ScraperJob.league_id == league.id, ScraperJob.kind=='update', ScraperJob.status.in_(['queued','running'])))
            if active:
                result.append(active)
            else:
                job = ScraperJob(kind='update', league_id=league.id, start_year=league.backfill_from_year, priority=0)
                session.add(job)
                result.append(job)
        await session.commit()
        return result


async def enqueue_due_updates(db=None, now=None):
    """Daily fallback if the external scheduler misses a run; never replace active work."""
    db=db or database;now=now or utcnow();created=[]
    async with db.session() as session:
        if session.bind.dialect.name=='postgresql':await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        for league in await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.priority,League.id)):
            if league.external_id in (315,348) and not await session.scalar(select(Match.id).where(Match.league_id==league.id).limit(1)):
                continue
            active=await session.scalar(select(ScraperJob.id).where(ScraperJob.league_id==league.id,ScraperJob.kind=='update',ScraperJob.status.in_(['running','queued'])).limit(1))
            if active:continue
            discoveries=(await session.execute(select(ScraperJob.discovery_finished_at,ScraperJob.discovery_metrics).where(ScraperJob.league_id==league.id,
                ScraperJob.kind.in_(['update','backfill']),ScraperJob.discovery_complete.is_(True),ScraperJob.discovery_finished_at>now-timedelta(hours=24)))).all()
            attempt=await session.scalar(select(func.max(ScraperJob.created_at)).where(ScraperJob.league_id==league.id,ScraperJob.kind=='update'))
            if any(not any(v.get('errors',0) for v in (metrics or {}).values()) for stamp,metrics in discoveries):continue
            if attempt and aware(attempt)>now-timedelta(hours=6):continue
            job=ScraperJob(kind='update',league_id=league.id,start_year=league.backfill_from_year,priority=0)
            session.add(job);created.append(job)
        await session.commit()
    return created

async def discover(job_id, client):
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        league = await session.get(League, job.league_id)
        if job.status in {'paused_disabled', 'paused_capacity', 'manual_paused'}:
            return
        session.info['historical_import'] = job.kind != 'update'
        if job.kind != 'update':
            await guard_history_write(session, force=True)
        kind, start_year, league_id, external_id = job.kind, job.start_year, league.id, league.external_id
        competition_type, schedule_format = league.competition_type, league.schedule_format
        if job.discovery_complete:
            # Previous UEFA cup discoveries persisted aggregate tie rows as
            # ScraperIssues. Replay only those old two-leg errors after the
            # verified nested-fixture parser update; existing match upserts
            # and job items are idempotent.
            if kind == 'backfill' and external_id in (103, 113, 2187):
                issues = (await session.scalars(select(ScraperIssue).where(
                    ScraperIssue.job_id == job.id))).all()
                def is_old_tie(issue):
                    row = (issue.raw or {}).get('schedule')
                    return (isinstance(row, list) and len(row) > 4
                            and isinstance(row[4], list) and row[4]
                            and all(isinstance(child, list) for child in row[4]))
                if any(is_old_tie(issue) for issue in issues):
                    job.discovery_complete = False
                    await session.commit()
                    log.info('[UEFA] rediscovering previously rejected two-leg ties job_id=%s', job.id)
                else:
                    return
            else:
                return
        if kind == 'stats_backfill':
            if job.match_id:
                await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': job.match_id})
                job.total_matches = 1
                job.discovery_complete = True
                await session.commit()
                return
            DISCOVERY_BATCH_SIZE = 50
            last_kickoff = None
            last_id = None
            total_discovered = await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id)) or 0
            while True:
                query = missing_statistics_query(league_id, start_year)
                if last_kickoff is not None and last_id is not None:
                    query = query.where(
                        or_(
                            Match.kickoff_at < last_kickoff,
                            and_(Match.kickoff_at == last_kickoff, Match.id < last_id),
                        )
                    )
                matches = (await session.scalars(
                    query.order_by(Match.kickoff_at.desc(), Match.id.desc()).limit(DISCOVERY_BATCH_SIZE)
                )).all()
                if not matches:
                    break
                for match in matches:
                    await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': match.id})
                    last_kickoff, last_id = match.kickoff_at, match.id
                    total_discovered += 1
                job.total_matches = total_discovered
                await session.commit()
                if len(matches) < DISCOVERY_BATCH_SIZE:
                    break
            job.total_matches = await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id))
            job.discovery_complete = True
            await session.commit()
            return
        if kind in ('market_backfill', 'market_odds'):
            if job.match_id:
                await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': job.match_id})
                job.total_matches = 1
                job.discovery_complete = True
                await session.commit()
                return
            pilot_cap = settings.market_odds_pilot_max_matches if settings.market_odds_pilot_enabled else None
            DISCOVERY_BATCH_SIZE = 50
            last_kickoff = None
            last_id = None
            total_discovered = await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id)) or 0
            while True:
                if pilot_cap is not None and total_discovered >= pilot_cap:
                    break
                query = missing_market_odds_query(league_id, start_year)
                # Pilot only: choose fixtures with meaningful near-term or historical
                # availability; distant future fixtures rarely have posted odds.
                if pilot_cap is not None:
                    query = query.where(Match.kickoff_at <= utcnow() + timedelta(days=7))
                if last_kickoff is not None and last_id is not None:
                    query = query.where(
                        or_(
                            Match.kickoff_at < last_kickoff,
                            and_(Match.kickoff_at == last_kickoff, Match.id < last_id),
                        )
                    )
                batch_limit = min(DISCOVERY_BATCH_SIZE, pilot_cap - total_discovered) if pilot_cap else DISCOVERY_BATCH_SIZE
                matches = (await session.scalars(
                    query.order_by(Match.kickoff_at.desc(), Match.id.desc()).limit(batch_limit)
                )).all()
                if not matches:
                    break
                for match in matches:
                    await get_or_create(session, JobItem, {'job_id': job.id, 'match_id': match.id})
                    last_kickoff, last_id = match.kickoff_at, match.id
                    total_discovered += 1
                job.total_matches = total_discovered
                await session.commit()
                if len(matches) < batch_limit:
                    break
            job.total_matches = await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id))
            job.discovery_complete = True
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
    async with database.session() as audit_session:
        fixtures_before = await audit_session.scalar(select(func.count(Match.id)).where(Match.league_id == league_id))
    seasons = await discover_seasons(client, external_id, start_year, overlap=True) if competition_type == 'national' or kind == 'domestic_history' else await discover_seasons(client, external_id, start_year)
    if not seasons:
        raise SourceError('No seasons available from the requested year')
    if kind == 'update':
        # Include the previous season for summer transitions and late corrections.
        seasons = seasons[-2:]
    now = utcnow()
    if kind == 'domestic_history':
        # Explicit archive-only repair, bounded to 2024..current year. Never
        # rescrape odds or rewrite an existing result/observation timestamp.
        seasons = [s for s in seasons if 2023 <= int(s[:4]) <= now.year][-4:]
    for season_name in seasons:
        payload = await season_data(client, external_id, season_name, schedule_format='cup') if schedule_format == 'cup' else await season_data(client, external_id, season_name)
        if payload.get('LeagueInfo'):
            from src.scrapers.goaloo.competitions import verified_snapshot, Competition, validate_identity
            source = next((record for record in verified_snapshot() if record['external_id'] == external_id), None)
            if source:
                validate_identity(Competition(**source), payload, season_name)
            elif kind == 'domestic_history':
                raise SourceError('Domestic history source is not in the verified catalog')
        elif kind == 'domestic_history':
            raise SourceError('Domestic history requires a verified competition identity')
        for round_number in discover_rounds(payload):
            await priority_cycle(database,client)
            log.info('[GOALOO] league=%s season=%s round=%s', external_id, season_name, round_number)
            async with database.session() as session:
                job = await session.get(ScraperJob, job_id)
                if not await league_enabled(session, job):
                    job.status = 'paused_disabled'
                    await session.commit()
                    return
                if kind != 'update' and session.bind.dialect.name == 'postgresql':
                    from .europe_capacity import check_capacity
                    try:
                        await check_capacity(session)
                    except HistoryCapacityError as exc:
                        await session.rollback()
                        job = await session.get(ScraperJob, job_id)
                        job.status, job.last_error = 'paused_capacity', str(exc)[:500]
                        await session.commit()
                        return
                session.info['historical_import'] = kind != 'update'
                job.current_season, job.current_round = season_name, round_number
                job.checkpoint_at = utcnow()
                # A transaction commits each round plus job items; rediscovery is idempotent.
                await session.execute(delete(ScraperIssue).where(ScraperIssue.job_id == job.id, ScraperIssue.season == season_name, ScraperIssue.round == round_number))
                errors = []
                counts = dict(discovered=0, added=0, updated=0, unchanged=0, errors=0)
                seen = set()
                for data in parse_matches(payload, round_number, errors):
                    if data['external_league_id'] != external_id:
                        errors.append(dict(external_match_id=str(data['external_match_id']),
                                           error='League ID does not match request', raw=data['raw']))
                        continue
                    if kind == 'domestic_history':
                        if (data['kickoff_at'] is None or data['kickoff_at'].year < 2024
                                or data['status'] != 'finished' or data['kickoff_at'] > now-timedelta(hours=3)):
                            continue
                        if await session.scalar(select(Match.id).where(Match.external_match_id==data['external_match_id'])):
                            continue
                    if competition_type == 'national' and (data['kickoff_at'] is None or data['kickoff_at'].year < start_year):
                        continue
                    if kind == 'update':
                        # A daily schedule can contain old seasons. Defer its old
                        # rows without preventing near-term fixtures/live results.
                        if counts.get('deferred_capacity') and historical(data.get('kickoff_at')):
                            counts['deferred_capacity'] += 1
                            continue
                        try:
                            async with session.begin_nested():
                                match = await store_match(session, league_id, season_name, data)
                        except HistoryCapacityError:
                            counts['deferred_capacity'] = counts.get('deferred_capacity', 0) + 1
                            continue
                    else:
                        match = await store_match(session, league_id, season_name, data)
                    if match.external_match_id not in seen:
                        seen.add(match.external_match_id)
                        counts['discovered'] += 1
                        counts[match._discovery_change] += 1
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
                counts['errors'] = len(errors)
                # Replace a round checkpoint on resume rather than double count it.
                job.discovery_metrics = {**(job.discovery_metrics or {}), f'{season_name}/{round_number}': counts}
                await session.flush()
                issues = await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id))
                job.total_matches = issues + await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id))
                await update_counts(session, job)
                await session.commit()
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        job.discovery_complete = True
        job.discovery_finished_at = utcnow()
        await session.commit()
    async with database.session() as audit_session:
        fixtures_after = await audit_session.scalar(select(func.count(Match.id)).where(Match.league_id == league_id))
    log.info('[DISCOVERY] league=%s kind=%s season_count=%s inserted=%s existing_total=%s',
             external_id,kind,len(seasons),fixtures_after-fixtures_before,fixtures_after)


async def run_job(job_id):
    log.info('[WORKER] job started id=%s', job_id)
    async with database.session() as session:
        job = await session.get(ScraperJob, job_id)
        if job.status in {'paused_disabled', 'paused_capacity', 'manual_paused'} or not await league_enabled(session, job):
            return
        if job.kind != 'update' and session.bind.dialect.name == 'postgresql':
            from .europe_capacity import check_capacity
            try:
                await check_capacity(session)
            except HistoryCapacityError as exc:
                await session.rollback()
                job = await session.get(ScraperJob, job_id)
                job.status, job.last_error = 'paused_capacity', str(exc)[:500]
                await session.commit()
                return
        job.status, job.started_at, job.finished_at = 'running', job.started_at or utcnow(), None
        job.checkpoint_at = utcnow()
        await session.commit()
    try:
        async with GoalooClient() as client:
            await priority_cycle(database, client)
            await discover(job_id, client)
            async with database.session() as session:
                if (await session.get(ScraperJob, job_id)).status in {'paused_capacity','paused_disabled'}:
                    return
            CHUNK_SIZE = HISTORY_SLICE_SIZE
            processed_slice = 0
            slice_started = time.monotonic()
            while True:
                async with database.session() as session:
                    item_ids = list(await session.scalars(
                        select(JobItem.id).where(
                            JobItem.job_id == job_id,
                            JobItem.status.in_(['queued', 'running']),
                        ).order_by(JobItem.id).limit(CHUNK_SIZE)
                    ))
                if not item_ids:
                    break
                for item_id in item_ids:
                    await wait_for_capacity()
                    await priority_cycle(database, client)
                    async with database.session() as session:
                        job = await session.get(ScraperJob, job_id)
                        if job.kind != 'update' and await session.scalar(select(ScraperJob.id).join(League).where(
                            League.enabled.is_(True), ScraperJob.kind == 'update', ScraperJob.status == 'queued'
                        ).limit(1)):
                            job.status = 'queued'
                            job.checkpoint_at = utcnow()
                            await session.commit()
                            return
                        if not await league_enabled(session, job):
                            job.status = 'paused_disabled'
                            await session.commit()
                            return
                        if job.kind != 'update' and session.bind.dialect.name == 'postgresql':
                            from .europe_capacity import check_capacity
                            try:
                                await check_capacity(session)
                            except HistoryCapacityError as exc:
                                await session.rollback()
                                job = await session.get(ScraperJob, job_id)
                                job.status = 'paused_capacity'
                                job.last_error = str(exc)[:500]
                                await session.commit()
                                return
                    await process_item(job_id, item_id, client)
                    async with database.session() as state_session:
                        if (await state_session.get(ScraperJob, job_id)).status in {'paused_capacity', 'paused_disabled'}:
                            return
                    processed_slice += 1
                    if job.kind != 'update' and (processed_slice >= HISTORY_SLICE_SIZE or time.monotonic()-slice_started >= 180):
                        await yield_job(job_id)
                        return
        async with database.session() as session:
            job = await session.get(ScraperJob, job_id)
            if not await league_enabled(session, job):
                job.status = 'paused_disabled'
                await session.commit()
                return
            await update_counts(session, job)
            job.status = 'partial' if job.failed_matches else 'completed'
            job.finished_at = utcnow()
            job.checkpoint_at = utcnow()
            await session.commit()
            log.info('[WORKER] job completed id=%s status=%s processed=%s failed=%s no_odds=%s',
                     job.id, job.status, job.processed_matches, job.failed_matches, getattr(job, 'no_odds_matches', 0))
        try:
            from src.analytics.performance import capture_upcoming
            async with database.session() as session:
                await capture_upcoming(session, job.league_id)
        except Exception as exc:
            log.warning('[PREDICTIONS] capture deferred: %s', type(exc).__name__)
    except HistoryCapacityError as exc:
        await pause_capacity(job_id, exc)
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
            if job.status in {'paused_disabled', 'paused_capacity', 'manual_paused'}:
                return
            session.info['historical_import'] = job.kind != 'update'
            await guard_history_write(session, match.kickoff_at)
            season = await session.get(Season, match.season_id)
            external_id, final = match.external_match_id, is_final(match)
            job.current_season, job.current_round = season.season_name, match.round
            job.checkpoint_at = utcnow()
            item.status = 'running'
            await session.commit()
            log.info('[MATCH] match_id=%s season=%s round=%s', external_id, season.season_name, match.round)
            league = await session.get(League, match.league_id)
            # expire_on_commit=False keeps the loaded metadata usable while releasing
            # the connection before potentially slow HTTP retries.
            await session.commit()
            if job.kind == 'stats_backfill':
                data = await fetch_statistics(client, external_id, league.external_id)
                await store_statistics(session, match, data)
                item.status = 'completed'
                item.error = None
            elif job.kind in ('market_backfill', 'market_odds'):
                odds_count = 0
                transient_errors = []
                ht_response_verified = False
                corner_response_verified = False

                # Independent check: only fetch HT goals if not already stored with meaningful line
                has_ht = await session.scalar(
                    select(MarketOdds.id).where(
                        MarketOdds.match_id == match.id,
                        MarketOdds.market == 'first_half_goals',
                        MarketOdds.period == 'first_half',
                        or_(
                            MarketOdds.latest_line.is_not(None),
                            MarketOdds.closing_line.is_not(None),
                        ),
                    ).limit(1)
                )
                if not has_ht or not final:
                    try:
                        ht_odds = await fetch_halftime_odds(client, external_id, final)
                        ht_response_verified = True
                        ht_ou = {
                            bk: m.get('ou', {})
                            for bk, m in ht_odds.items()
                            if 'ou' in m and (
                                m.get('ou', {}).get('latest_line') is not None or
                                m.get('ou', {}).get('opening_line') is not None or
                                m.get('ou', {}).get('closing_line') is not None
                            )
                        }
                        if ht_ou:
                            odds_count += await store_market_odds(
                                session, match, ht_ou, market='first_half_goals', period='first_half'
                            )
                    except (SourceError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
                        log.warning('[MARKET_ODDS] half-time odds transient failure match_id=%s: %s', external_id, exc)
                        transient_errors.append(f'HT odds: {type(exc).__name__}: {str(exc)[:150]}')
                    except HistoryCapacityError:
                        raise
                    except Exception as exc:
                        log.warning('[MARKET_ODDS] half-time odds error match_id=%s: %s', external_id, exc)
                        transient_errors.append(f'HT odds: {type(exc).__name__}: {str(exc)[:150]}')
                else:
                    ht_response_verified = True

                # Independent check: only fetch corner odds if not already stored with meaningful line
                has_corners = await session.scalar(
                    select(MarketOdds.id).where(
                        MarketOdds.match_id == match.id,
                        MarketOdds.market == 'corners',
                        MarketOdds.period == 'full_time',
                        or_(
                            MarketOdds.latest_line.is_not(None),
                            MarketOdds.closing_line.is_not(None),
                        ),
                    ).limit(1)
                )
                if not has_corners or not final:
                    try:
                        corner_odds = await fetch_corner_odds(client, external_id, half_time=False, final=final)
                        corner_response_verified = True
                        corner_valid = {
                            bk: data
                            for bk, data in corner_odds.items()
                            if (
                                data.get('latest_line') is not None or
                                data.get('opening_line') is not None or
                                data.get('closing_line') is not None
                            )
                        }
                        if corner_valid:
                            odds_count += await store_market_odds(
                                session, match, corner_valid, market='corners', period='full_time'
                            )
                    except (SourceError, httpx.HTTPError, TimeoutError, asyncio.TimeoutError) as exc:
                        log.warning('[MARKET_ODDS] corner odds transient failure match_id=%s: %s', external_id, exc)
                        transient_errors.append(f'Corner odds: {type(exc).__name__}: {str(exc)[:150]}')
                    except HistoryCapacityError:
                        raise
                    except Exception as exc:
                        log.warning('[MARKET_ODDS] corner odds error match_id=%s: %s', external_id, exc)
                        transient_errors.append(f'Corner odds: {type(exc).__name__}: {str(exc)[:150]}')
                else:
                    corner_response_verified = True

                await session.commit()
                if transient_errors:
                    item.status = 'failed'
                    item.error = ('; '.join(transient_errors) + f' match_id={external_id}')[:500]
                elif odds_count > 0 or has_ht or has_corners:
                    item.status = 'completed'
                    item.error = None
                elif ht_response_verified and corner_response_verified:
                    item.status = 'no_odds'
                    item.error = 'No verified half-time or corner odds in source'
                else:
                    item.status = 'failed'
                    item.error = f'Unverified market responses match_id={external_id}'
            else:
                errors = []
                try:
                    odds = await fetch_odds(client, external_id, final)
                    await store_odds(session, match, odds, final)
                    await session.commit()  # A later source failure cannot roll back valid odds.
                    ok = complete_odds(odds, final)
                    if not ok:
                        from src.scrapers.goaloo.odds import odds_gaps
                        gaps=odds_gaps(odds,final)
                        errors.append('Incomplete bookmaker markets: '+', '.join(f"{g['bookmaker']}/{g['market']}/{g['stage']}" for g in gaps))
                except SourceError as exc:
                    ok = False
                    errors.append(str(exc))
                # Extended markets from verified Goaloo endpoints
                try:
                    ht_odds = await fetch_halftime_odds(client, external_id, final)
                    ht_ou = {bk: m.get('ou', {}) for bk, m in ht_odds.items() if 'ou' in m}
                    if ht_ou:
                        await store_market_odds(session, match, ht_ou, market='first_half_goals', period='first_half')
                        await session.commit()
                except SourceError as exc:
                    log.warning('[MARKET_ODDS] half-time odds unavailable match_id=%s: %s', external_id, exc)
                try:
                    corner_odds = await fetch_corner_odds(client, external_id, half_time=False, final=final)
                    if corner_odds:
                        await store_market_odds(session, match, corner_odds, market='corners', period='full_time')
                        await session.commit()
                except SourceError as exc:
                    log.warning('[MARKET_ODDS] corner odds unavailable match_id=%s: %s', external_id, exc)
                if match.status == 'finished' or match.status in LIVE_STATUSES:
                    try:
                        data = await fetch_statistics(client, external_id, league.external_id)
                        await store_statistics(session, match, data)
                        await session.commit()
                    except SourceError:
                        log.warning('[STATISTICS] source unavailable match_id=%s; dedicated stats job will retry', external_id)
                if settings.save_snapshots:
                    bookmakers = (await session.scalars(select(Bookmaker))).all()
                    await session.commit()
                    for bookmaker in bookmakers:
                        try:
                            history = await fetch_history(client, external_id, bookmaker.external_id)
                            await store_history(session, match.id, bookmaker.id, history)
                            await session.commit()
                        except SourceError as exc:
                            ok = False
                            errors.append('History: ' + str(exc))
                item.status = 'completed' if ok else 'failed'
                item.error = None if ok else ('; '.join(errors) + f' match_id={external_id}')[:500]
            await session.flush()
            await update_counts(session, job)
            job.checkpoint_at = utcnow()
            if item.status == 'failed':
                job.last_error = item.error
            await session.commit()
            log.info('[JOB] job_id=%s processed=%s/%s failed=%s', job.id, job.processed_matches, job.total_matches, job.failed_matches)
    except HistoryCapacityError as exc:
        await pause_capacity(job_id, exc)
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
    job.no_odds_matches = counts.get('no_odds', 0)
    issues = await session.scalar(select(func.count(ScraperIssue.id)).where(ScraperIssue.job_id == job.id))
    job.failed_matches = counts.get('failed', 0) + issues


async def work(once=False):
    initialization_checked = False
    stats_queue_checked = float('-inf')
    market_queue_checked = float('-inf')
    update_queue_checked = float('-inf')
    while True:
        try:
            await wait_for_capacity()
            if not database.ready:
                await database.initialize()
            if not initialization_checked:
                # Validate the three European cup schedules against the source before
                # they are enabled or any historical backfill is queued.
                try:
                    if not settings.auto_backfill_on_empty:
                        raise ValueError('UEFA onboarding is disabled with automatic backfill')
                    from src.scrapers.goaloo.competitions import verify_uefa_competitions, upsert_competitions
                    async with GoalooClient() as uefa_client:
                        verified_uefa = await verify_uefa_competitions(uefa_client)
                    async with database.session() as uefa_session:
                        if uefa_session.bind.dialect.name == 'postgresql':
                            await uefa_session.execute(text('SELECT pg_advisory_xact_lock(213001)'))
                        await upsert_competitions(uefa_session, verified_uefa)
                        await uefa_session.commit()
                    log.info('[UEFA] verified and enabled CL EL Conference, queueing 2024+ backfill')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning('[UEFA] onboarding deferred error=%s', type(exc).__name__)
                await enqueue_initial_backfill(database)
                await enqueue_catalog_backfills(database)
                initialization_checked = True
            if settings.auto_backfill_on_empty and database.engine.dialect.name == 'postgresql':
                now_mono = time.monotonic()
                if now_mono - stats_queue_checked >= 60:
                    await enqueue_stats_rollout(automatic=True)
                    stats_queue_checked = now_mono
                if now_mono - market_queue_checked >= 60:
                    await enqueue_market_odds_rollout(automatic=True)
                    market_queue_checked = now_mono
            if not once and settings.app_env=='production' and time.monotonic()-update_queue_checked>=300:
                await enqueue_due_updates(database)
                update_queue_checked=time.monotonic()
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
                        await reconcile_owned_queue(session)
                        job_id = await session.scalar(next_job_query())
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
            if previous and previous.status in {'queued','running','paused_capacity','paused_disabled'}:
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


async def enqueue_market_odds_rollout(automatic=False, pilot=None):
    """Progressive, resumable market odds backfill.

    If pilot is True (or default for automatic reconciliation when pilot mode is enabled),
    only the configured pilot league (default external_id=36) is enqueued.
    The 8-league rollout runs only when pilot is False or explicitly requested.
    Runs serially with low memory footprint, suitable for Render free tier.
    """
    from src.scrapers.goaloo.competitions import PRODUCTION_LEAGUE_IDS
    if pilot is None:
        pilot = settings.market_odds_pilot_enabled if automatic else False

    target_league_ids = [settings.market_odds_pilot_league] if pilot else PRODUCTION_LEAGUE_IDS

    async with database.session() as session:
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
        jobs = []
        for priority, external_id in enumerate(target_league_ids, 1):
            league = await session.scalar(select(League).where(League.enabled.is_(True), League.external_id == external_id))
            if not league:
                continue
            previous = await session.scalar(select(ScraperJob).where(
                ScraperJob.league_id == league.id,
                ScraperJob.kind.in_(['market_backfill', 'market_odds']),
                ScraperJob.match_id.is_(None),
            ).order_by(ScraperJob.id.desc()).limit(1))
            if previous and previous.status in {'queued', 'running','paused_capacity','paused_disabled'}:
                jobs.append(previous)
                continue
            if previous and previous.status in {'partial', 'failed'}:
                if automatic and previous.finished_at and aware(previous.finished_at) > utcnow() - timedelta(days=1):
                    jobs.append(previous)
                    continue
                for item in (await session.scalars(select(JobItem).where(
                    JobItem.job_id == previous.id,
                    JobItem.status.notin_(['completed', 'no_odds']),
                ))).all():
                    item.status, item.error = 'queued', None
                previous.status, previous.finished_at, previous.last_error = 'queued', None, None
                previous.failed_matches = 0
                jobs.append(previous)
                continue
            # Backfill retries are not a live quote-refresh loop. A completed zero-work
            # pass must not create thousands of new jobs while unavailable markets
            # continue to satisfy the "missing" predicate.
            if automatic and previous and previous.finished_at and aware(previous.finished_at) > utcnow() - timedelta(days=1):
                jobs.append(previous)
                continue
            if not await session.scalar(missing_market_odds_query(league.id).with_only_columns(Match.id).limit(1)):
                if previous:
                    jobs.append(previous)
                continue
            job = ScraperJob(kind='market_backfill', league_id=league.id, start_year=2024, priority=priority + 20)
            session.add(job)
            jobs.append(job)
        await session.commit()
        return jobs

