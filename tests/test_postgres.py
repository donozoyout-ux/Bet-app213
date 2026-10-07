"""Opt-in real PostgreSQL validation; CI supplies an isolated database."""
import os
import asyncio
import uuid
import pytest
from sqlalchemy import select, text
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.db import Database
from src.models import League, ScraperJob
from src.jobs.storage import store_match, store_odds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import parse_odds
from .test_goaloo import fixture
from src.jobs.worker import enqueue_initial_backfill


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


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_parallel_startup_enqueues_one_backfill():
    db=Database(os.environ['TEST_POSTGRES_URL'])
    schema='betapp_auto_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        jobs=await asyncio.gather(*[enqueue_initial_backfill(db,enabled=True) for _ in range(6)])
        assert len({job.id for job in jobs})==1
        assert await enqueue_initial_backfill(db,enabled=False) is None
        async with db.session() as session:
            league=await session.scalar(select(League))
            await store_match(session,league.id,'2024-2025',next(parse_matches(fixture('goaloo_league.json'),1)))
            await session.commit()
        assert await enqueue_initial_backfill(db,enabled=True) is None
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
