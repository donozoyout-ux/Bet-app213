"""Opt-in real PostgreSQL validation; CI supplies an isolated database."""
import os
import pytest
from sqlalchemy import select, text
from src.db import Database
from src.models import League, ScraperJob
from src.jobs.storage import store_match, store_odds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import parse_odds
from .test_goaloo import fixture


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_init_storage_and_exclusive_worker_lock(monkeypatch):
    db = Database(os.environ['TEST_POSTGRES_URL'])
    try:
        await db.initialize()
        await db.initialize()
        async with db.session() as session:
            league = await session.scalar(select(League).where(League.external_id==36))
            match = await store_match(session,league.id,'2024-2025',next(parse_matches(fixture('goaloo_league.json'),1)))
            await store_odds(session,match,parse_odds(fixture('goaloo_odds.json'),True),True)
            await session.commit()
            assert match.odds_complete
        async with db.engine.connect() as first, db.engine.connect() as second:
            assert await first.scalar(text('SELECT pg_try_advisory_lock(213002)'))
            assert not await second.scalar(text('SELECT pg_try_advisory_lock(213002)'))
            assert await first.scalar(text('SELECT pg_advisory_unlock(213002)'))
    finally:
        await db.close()
