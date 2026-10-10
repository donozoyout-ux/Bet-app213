import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func

from src.models import Match, League, Season, Bookmaker, ScraperJob, JobItem, MarketOdds, Team, utcnow
from src.jobs.worker import (
    enqueue,
    run_job,
    discover,
    missing_market_odds_query,
    enqueue_market_odds_rollout,
)
from src.scrapers.goaloo.odds import parse_odds
from src.scrapers.goaloo.corner_odds import parse_corner_odds
from src.scrapers.goaloo.client import SourceError, GoalooClient
from .test_goaloo import fixture


async def setup_test_match(session, external_match_id=2996166, league_ext_id=16, league_name='Holland Eredivisie', league_country='Netherlands'):
    league = await session.scalar(select(League).where(League.external_id == league_ext_id))
    if not league:
        league = League(external_id=league_ext_id, name=league_name, country=league_country, enabled=True)
        session.add(league)
        await session.flush()
    season = await session.scalar(select(Season).where(Season.league_id == league.id, Season.season_name == '2024-2025'))
    if not season:
        season = Season(league_id=league.id, season_name='2024-2025')
        session.add(season)
        await session.flush()
    for cid, name in [(3, 'Crown'), (8, 'Bet365'), (31, 'Sbobet')]:
        b = await session.scalar(select(Bookmaker).where(Bookmaker.name == name))
        if not b:
            session.add(Bookmaker(external_id=cid, name=name))
    await session.flush()

    match = await session.scalar(select(Match).where(Match.external_match_id == external_match_id))
    if not match:
        h_team = await session.scalar(select(Team).where(Team.external_id == 101))
        if not h_team:
            h_team = Team(external_id=101, name='Home Team')
            session.add(h_team)
        a_team = await session.scalar(select(Team).where(Team.external_id == 102))
        if not a_team:
            a_team = Team(external_id=102, name='Away Team')
            session.add(a_team)
        await session.flush()
        match = Match(
            external_match_id=external_match_id,
            league_id=league.id,
            season_id=season.id,
            round=1,
            home_team_id=h_team.id,
            away_team_id=a_team.id,
            status='finished',
            ht_home=1,
            ht_away=0,
            ft_home=2,
            ft_away=1,
            kickoff_at=datetime(2024, 8, 17, 14, 0, tzinfo=timezone.utc),
        )
        session.add(match)
        await session.flush()
    return match, league


async def test_pilot_mode_rollout_controls(db, monkeypatch):
    import src.jobs.worker as worker
    from src.scrapers.goaloo.competitions import PRODUCTION_LEAGUE_IDS
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        for ext_id in PRODUCTION_LEAGUE_IDS:
            league = await session.scalar(select(League).where(League.external_id == ext_id))
            if not league:
                session.add(League(external_id=ext_id, name=f'League {ext_id}', country='Test', enabled=True))
        await session.commit()

    # Pilot mode enqueues only the pilot league (default 36)
    pilot_jobs = await enqueue_market_odds_rollout(pilot=True)
    assert len(pilot_jobs) == 1
    async with db.session() as session:
        league = await session.get(League, pilot_jobs[0].league_id)
        assert league.external_id == 36

    # Full rollout enqueues all 8 enabled leagues
    full_jobs = await enqueue_market_odds_rollout(pilot=False)
    assert len(full_jobs) == len(PRODUCTION_LEAGUE_IDS)


async def test_match_2996166_real_league_identity_and_fixture_backfill(db, monkeypatch):
    """Verify match 2996166 with its actual Holland Eredivisie (external_id 16) identity."""
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        match, league = await setup_test_match(
            session,
            external_match_id=2996166,
            league_ext_id=16,
            league_name='Holland Eredivisie',
            league_country='Netherlands',
        )
        await session.commit()

    assert league.external_id == 16
    assert league.name == 'Holland Eredivisie'

    # Mock Goaloo scrapers with verified fixtures for match 2996166
    async def mock_ht(client, ext_id, final=False):
        assert ext_id == 2996166
        return parse_odds(fixture('goaloo_halftime_odds_2996166.json'), final)

    async def mock_corners(client, ext_id, half_time=False, final=False):
        assert ext_id == 2996166
        return parse_corner_odds(fixture('goaloo_corner_odds_2996166.json'), final)

    monkeypatch.setattr(worker, 'fetch_halftime_odds', mock_ht)
    monkeypatch.setattr(worker, 'fetch_corner_odds', mock_corners)

    job = await enqueue('market_backfill', league_external_id=16, match_id=match.id)
    await run_job(job.id)

    async with db.session() as session:
        stored_job = await session.get(ScraperJob, job.id)
        assert stored_job.status == 'completed'
        assert stored_job.processed_matches == 1
        assert stored_job.failed_matches == 0
        assert stored_job.no_odds_matches == 0

        # Verify HT goals odds saved for Bet365, Crown, Sbobet
        ht_rows = (await session.scalars(
            select(MarketOdds).where(
                MarketOdds.match_id == match.id,
                MarketOdds.market == 'first_half_goals',
                MarketOdds.period == 'first_half',
            )
        )).all()
        assert len(ht_rows) == 3

        # Verify Corner odds saved for Bet365 (Crown/Sbobet not in this fixture)
        corner_rows = (await session.scalars(
            select(MarketOdds).where(
                MarketOdds.match_id == match.id,
                MarketOdds.market == 'corners',
                MarketOdds.period == 'full_time',
            )
        )).all()
        assert len(corner_rows) == 1
        assert corner_rows[0].latest_line == 10.0
        assert corner_rows[0].latest_over == 1.85
        assert corner_rows[0].latest_under == 1.95


async def test_paginated_discovery_and_pilot_match_cap(db, monkeypatch):
    """Verify keyset paginated discovery and strict pilot match cap."""
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        for i in range(1, 26):
            await setup_test_match(
                session,
                external_match_id=3100000 + i,
                league_ext_id=36,
                league_name='Premier League',
                league_country='England',
            )
        await session.commit()

    # In pilot mode: discovery must cap items to pilot max (e.g. 10)
    job = await enqueue('market_backfill', league_external_id=36)
    async with GoalooClient() as client:
        await discover(job.id, client)

    async with db.session() as session:
        job_db = await session.get(ScraperJob, job.id)
        assert job_db.discovery_complete is True
        assert job_db.total_matches == 10
        item_count = await session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id))
        assert item_count == 10


async def test_meaningful_odds_data_requirement(db, monkeypatch):
    """Verify market completeness requires meaningful lines/prices, not empty placeholder rows."""
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        match, league = await setup_test_match(session, external_match_id=2996170, league_ext_id=36)
        b365 = await session.scalar(select(Bookmaker).where(Bookmaker.name == 'Bet365'))
        # Row with all NULL lines is NOT meaningful
        corrupted_row = MarketOdds(
            match_id=match.id,
            bookmaker_id=b365.id,
            market='first_half_goals',
            period='first_half',
            opening_line=None,
            latest_line=None,
            closing_line=None,
            raw={},
        )
        session.add(corrupted_row)
        await session.commit()

    # The match must still be discovered because the row lacks meaningful lines!
    async with db.session() as session:
        missing = (await session.scalars(missing_market_odds_query(league.id))).all()
        assert match.id in [m.id for m in missing]


async def test_independent_market_discovery_and_partial_odds(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        match, league = await setup_test_match(session, external_match_id=2996167, league_ext_id=36)
        b365 = await session.scalar(select(Bookmaker).where(Bookmaker.name == 'Bet365'))
        # Store verified HT goals with valid line
        mo = MarketOdds(
            match_id=match.id,
            bookmaker_id=b365.id,
            market='first_half_goals',
            period='first_half',
            opening_line=1.25,
            latest_line=1.25,
            raw={},
        )
        session.add(mo)
        await session.commit()

    # Match has HT goals, but lacks Corner odds -> MUST be discovered by missing_market_odds_query!
    async with db.session() as session:
        missing = (await session.scalars(missing_market_odds_query(league.id))).all()
        assert match.id in [m.id for m in missing]

    # Process: HT is already present so fetch_halftime_odds is skipped, Corner odds are fetched and stored
    calls = []
    async def mock_ht(client, ext_id, final=False):
        calls.append(('ht', ext_id))
        return {}

    async def mock_corners(client, ext_id, half_time=False, final=False):
        calls.append(('corners', ext_id))
        return parse_corner_odds(fixture('goaloo_corner_odds_2996166.json'), final)

    monkeypatch.setattr(worker, 'fetch_halftime_odds', mock_ht)
    monkeypatch.setattr(worker, 'fetch_corner_odds', mock_corners)

    job = await enqueue('market_backfill', league_external_id=36, match_id=match.id)
    await run_job(job.id)

    # HT fetch was skipped because match already has final HT odds stored
    assert ('corners', 2996167) in calls

    async with db.session() as session:
        corner_rows = (await session.scalars(
            select(MarketOdds).where(
                MarketOdds.match_id == match.id,
                MarketOdds.market == 'corners',
            )
        )).all()
        assert len(corner_rows) == 1


async def test_transient_error_is_marked_failed_not_no_odds(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        match, league = await setup_test_match(session, external_match_id=2996168, league_ext_id=36)
        await session.commit()

    # Simulate Goaloo timeout / SourceError on HT odds
    async def failing_ht(client, ext_id, final=False):
        raise SourceError('Goaloo connection timed out after 3 attempts')

    async def empty_corners(client, ext_id, half_time=False, final=False):
        return {}

    monkeypatch.setattr(worker, 'fetch_halftime_odds', failing_ht)
    monkeypatch.setattr(worker, 'fetch_corner_odds', empty_corners)

    job = await enqueue('market_backfill', league_external_id=36, match_id=match.id)
    await run_job(job.id)

    async with db.session() as session:
        stored_job = await session.get(ScraperJob, job.id)
        assert stored_job.status == 'partial'
        assert stored_job.failed_matches == 1
        assert stored_job.no_odds_matches == 0  # NOT marked as no_odds!
        item = await session.scalar(select(JobItem).where(JobItem.job_id == job.id))
        assert item.status == 'failed'
        assert 'SourceError' in item.error

    # Resume retries the failed item
    async def fixed_ht(client, ext_id, final=False):
        return parse_odds(fixture('goaloo_halftime_odds_2996166.json'), final)

    monkeypatch.setattr(worker, 'fetch_halftime_odds', fixed_ht)
    await enqueue('market_backfill', league_external_id=36, resume_id=job.id)
    await run_job(job.id)

    async with db.session() as session:
        stored_job = await session.get(ScraperJob, job.id)
        assert stored_job.status == 'completed'
        assert stored_job.failed_matches == 0
        assert stored_job.processed_matches == 1


async def test_real_no_odds_handling_and_cooldown_retry(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    async with db.session() as session:
        match, league = await setup_test_match(session, external_match_id=2996169, league_ext_id=36)
        await session.commit()

    # Valid HTTP 200 OK responses with empty odds lists (real no-odds case)
    async def empty_ht(client, ext_id, final=False):
        return {}

    async def empty_corners(client, ext_id, half_time=False, final=False):
        return {}

    monkeypatch.setattr(worker, 'fetch_halftime_odds', empty_ht)
    monkeypatch.setattr(worker, 'fetch_corner_odds', empty_corners)

    job = await enqueue('market_backfill', league_external_id=36, match_id=match.id)
    await run_job(job.id)

    async with db.session() as session:
        stored_job = await session.get(ScraperJob, job.id)
        assert stored_job.status == 'completed'
        assert stored_job.processed_matches == 0
        assert stored_job.no_odds_matches == 1
        assert stored_job.failed_matches == 0
        item = await session.scalar(select(JobItem).where(JobItem.job_id == job.id))
        assert item.status == 'no_odds'

        # Within 7-day cooldown: match is NOT re-discovered
        missing_now = (await session.scalars(missing_market_odds_query(league.id))).all()
        assert match.id not in [m.id for m in missing_now]

        # Simulate job finished 8 days ago (past cooldown)
        stored_job.finished_at = utcnow() - timedelta(days=8)
        stored_job.created_at = utcnow() - timedelta(days=8)
        await session.commit()

        # Past cooldown: match IS re-discovered for controlled retry
        missing_later = (await session.scalars(missing_market_odds_query(league.id))).all()
        assert match.id in [m.id for m in missing_later]


async def test_chunked_job_execution_with_recovery(db, monkeypatch):
    import dataclasses
    import src.jobs.worker as worker
    monkeypatch.setattr(worker, 'database', db)

    # Disable pilot limit to test 30-match chunking with frozen settings replacement
    monkeypatch.setattr(worker, 'settings', dataclasses.replace(worker.settings, market_odds_pilot_enabled=False))

    match_ids = []
    async with db.session() as session:
        for i in range(1, 31):
            m, league = await setup_test_match(session, external_match_id=3000000 + i, league_ext_id=36)
            match_ids.append(m.id)
        await session.commit()


    processed_calls = []
    async def mock_ht(client, ext_id, final=False):
        processed_calls.append(ext_id)
        return parse_odds(fixture('goaloo_halftime_odds_2996166.json'), final)

    async def mock_corners(client, ext_id, half_time=False, final=False):
        return parse_corner_odds(fixture('goaloo_corner_odds_2996166.json'), final)

    monkeypatch.setattr(worker, 'fetch_halftime_odds', mock_ht)
    monkeypatch.setattr(worker, 'fetch_corner_odds', mock_corners)

    # Queue backfill for the league
    job = await enqueue('market_backfill', league_external_id=36)
    await run_job(job.id)

    assert len(processed_calls) == 30
    async with db.session() as session:
        stored_job = await session.get(ScraperJob, job.id)
        assert stored_job.status == 'completed'
        assert stored_job.processed_matches == 30
        assert stored_job.failed_matches == 0
