"""Deterministic read budgets; synthetic fixtures only, never production writes."""
import os
import uuid
import json
import statistics
import time
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from src.db import Database
from src.models import Match
from src.query_profile import profile, metrics
from tests.test_predictions import seed, NOW
from tests.test_daily_pool import pool_fixture
from httpx import AsyncClient, ASGITransport


@pytest.fixture(params=['sqlite', 'postgresql'])
async def perf_db(request, db):
    if request.param == 'sqlite':
        yield db
        return
    if not os.getenv('TEST_POSTGRES_URL'):
        pytest.skip('TEST_POSTGRES_URL is not configured')
    other = Database(os.environ['TEST_POSTGRES_URL'])
    schema = 'perf104_' + uuid.uuid4().hex
    try:
        async with other.engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        other.engine = other.engine.execution_options(schema_translate_map={None: schema})
        other.sessions = async_sessionmaker(other.engine, expire_on_commit=False)
        await other.initialize()
        yield other
    finally:
        async with other.engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True))
        await other.close()


async def test_upcoming_query_budget_and_payload_parity(perf_db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await seed(perf_db)
    async with perf_db.session() as session:
        match = await session.scalar(select(Match).where(Match.status == 'scheduled'))
        match.raw = {'large_source_evidence': 'x' * 65536}
        await session.commit()
    queries = []
    def capture(conn, cursor, statement, *args):
        queries.append(statement)
    event.listen(perf_db.engine.sync_engine, 'before_cursor_execute', capture)
    try:
        async def get(**kwargs):
            # Explicit Query defaults because this exercises the handler directly.
            async with perf_db.session() as session:
                return await routes.matches(session=session, view='upcoming', round=None,
                    limit=12, offset=0, upcoming_days=7, **kwargs)
        with profile('issue104_upcoming'):
            result = await get()
            counts = dict(metrics.get())
        print('upcoming_sql', sum(n for n, _ in counts.values()))
        assert result['total'] == 1 and len(result['items']) == 1
        assert 'raw' not in result['items'][0]
        assert not result['upcoming_expanded']
        assert len(queries) == 2, len(queries)
        assert 'matches.raw' not in queries[-1]
        async with perf_db.session() as session:
            match = await session.scalar(select(Match).where(Match.status == 'scheduled'))
            match.kickoff_at = NOW + timedelta(days=20)
            await session.commit()
        expanded = await get()
        assert expanded['total'] == 1 and expanded['upcoming_expanded']
        async with perf_db.session() as session:
            match = await session.scalar(select(Match).where(Match.status == 'scheduled'))
            match.status = 'finished'
            await session.commit()
        empty = await get()
        assert empty['items'] == [] and empty['total'] == 0
    finally:
        event.remove(perf_db.engine.sync_engine, 'before_cursor_execute', capture)


async def test_daily_budget_and_immediate_expiry(perf_db):
    from src.analytics.publications import publish_daily, read_daily
    await pool_fixture(perf_db, 6)
    async with perf_db.session() as session:
        published = await publish_daily(session, NOW)
        await session.commit()
    assert published['items']
    with profile('issue104_daily'):
        async with perf_db.session() as session:
            result = await read_daily(session, NOW)
        counts = dict(metrics.get())
    count = sum(n for n, _ in counts.values())
    print('daily_sql', count)
    assert count == 2
    assert result['items'] == published['items']
    async with perf_db.session() as session:
        match = await session.get(Match, result['items'][0]['match']['id'])
        match.status = 'live'
        await session.commit()
    async with perf_db.session() as session:
        assert (await read_daily(session, NOW))['items'] == []
        assert (await read_daily(session, NOW+timedelta(minutes=16)))['items'] == []


async def test_read_endpoint_sample_report(perf_db, monkeypatch):
    """21 sequential reads per route, SQL budgets rather than flaky timing gates."""
    import src.api.routes as routes
    from src.api.main import app
    from src.analytics.publications import publish_daily
    monkeypatch.setattr(routes, 'database', perf_db)
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await pool_fixture(perf_db, 6)
    async with perf_db.session() as session:
        await publish_daily(session, NOW)
        await session.commit()
        match_id = await session.scalar(select(Match.id).where(Match.status == 'scheduled'))
    async def dependency():
        async with perf_db.session() as session:
            yield session
    old = dict(app.dependency_overrides)
    app.dependency_overrides[routes.session_dependency] = dependency
    report = {}
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(perf_db.engine.sync_engine, 'before_cursor_execute', capture)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for path in ['/api/matches?view=upcoming&limit=12&include_odds=true',
                         '/api/predictions', '/api/predictions/best', '/api/coupons/daily',
                         '/api/dashboard/revision', '/api/market-performance?limit=1',
                         f'/api/matches/{match_id}']:
                elapsed, counts = [], []
                for _ in range(21):
                    started = time.perf_counter()
                    statements.clear()
                    with profile('issue104_sample'):
                        response = await client.get(path)
                        counts.append(len(statements))
                    elapsed.append(round(1000*(time.perf_counter()-started), 2))
                    assert response.status_code == 200, response.text
                report[path] = dict(first_ms=elapsed[0], warm_n=20,
                    warm_median_ms=statistics.median(elapsed[1:]), warm_p95_ms=sorted(elapsed[1:])[18],
                    sql_min=min(counts), sql_max=max(counts), errors=0)
                if path.startswith('/api/matches?'):
                    assert max(counts) == 6  # Count + list + four batched odds tables.
                if path == '/api/coupons/daily':
                    assert max(counts) == 2
        print('ISSUE104_SAMPLE', perf_db.engine.dialect.name, json.dumps(report, sort_keys=True))
    finally:
        event.remove(perf_db.engine.sync_engine, 'before_cursor_execute', capture)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)
