"""Incident regressions: contention, bounded caching and real PostgreSQL workload."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import event, select, insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from src.api.read_cache import ReadCache
from src.db import Database
from src.models import Match, MatchStatistics, League, Season, ScraperJob, JobItem
from tests.test_predictions import seed, NOW


class Engine:
    dialect = SimpleNamespace(name='postgresql')


async def test_read_cache_coalesces_and_does_not_share_mutable_results():
    cache, engine = ReadCache(), Engine()
    calls = 0
    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(.02)
        return {'items': [1]}
    results = await asyncio.gather(*[cache.get(engine, 'board', compute) for _ in range(12)])
    assert calls == 1
    results[0]['items'].clear()
    assert (await cache.get(engine, 'board', compute))['items'] == [1]


async def test_read_cache_cancellation_releases_gate_and_errors_are_not_cached():
    cache, engine = ReadCache(), Engine()
    entered = asyncio.Event()
    async def stuck():
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(cache.get(engine, 'board', stuck))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async def failed():
        raise ValueError('transient')
    with pytest.raises(ValueError):
        await cache.get(engine, 'board', failed)
    async def recovered():
        return 42
    assert await asyncio.wait_for(cache.get(engine, 'board', recovered), .5) == 42


def test_worker_memory_pressure_is_cooperative(tmp_path, monkeypatch):
    from src.jobs.resources import memory_constrained
    monkeypatch.setenv('BETAPP_PROCESS_ROLE', 'scraper')
    (tmp_path/'memory.current').write_text('90')
    (tmp_path/'memory.max').write_text('100')
    assert memory_constrained(tmp_path)
    (tmp_path/'memory.current').write_text('50')
    assert not memory_constrained(tmp_path)


async def test_board_batches_selects_independent_of_match_count(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    _, league, season, teams, _ = await seed(db)
    async with db.session() as session:
        session.add_all([Match(external_match_id=1000000+i,league_id=league,season_id=season,
            round=2,home_team_id=teams[0],away_team_id=teams[1],status='scheduled',
            kickoff_at=NOW+timedelta(days=2),raw={}) for i in range(25)])
        await session.commit()
    queries = []
    def count(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().upper().startswith('SELECT'):queries.append(statement)
    event.listen(db.engine.sync_engine, 'before_cursor_execute', count)
    try:
        response = await api.get('/api/predictions/best')
        assert response.status_code == 200, response.text
        assert response.json()['evaluated_matches'] == 26
        assert len(queries) <= 12, len(queries)
    finally:
        event.remove(db.engine.sync_engine, 'before_cursor_execute', count)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_10161_matches_concurrent_api_during_stats_backfill(monkeypatch):
    import src.api.routes as routes
    import src.jobs.worker as worker
    from src.api.main import app
    from src.scrapers.goaloo.competitions import apply_production_scope
    from tests.test_match_statistics import captured
    db = Database(os.environ['TEST_POSTGRES_URL'])
    schema = 'betapp_latency_' + uuid.uuid4().hex
    scraper_db = None
    task = None
    release = asyncio.Event()
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine = db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions = async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        _, league, season, teams, history = await seed(db)
        async with db.session() as session:
            await apply_production_scope(session)
            leagues = (await session.scalars(select(League).where(League.enabled))).all()
            assert len(leagues) == 8
            seasons = {league:season}
            for l in leagues:
                if l.id == league:continue
                s = Season(league_id=l.id,season_name='2026-2027')
                session.add(s);await session.flush();seasons[l.id] = s.id
            records=[]
            for i in range(10080):
                lid = leagues[i%8].id
                when = NOW+timedelta(days=2) if i>=10000 else NOW-timedelta(days=2+i%900)
                records.append(dict(external_match_id=2000000+i,league_id=lid,season_id=seasons[lid],
                    round=1,home_team_id=teams[i%2],away_team_id=teams[1-i%2],
                    status='scheduled' if i>=10000 else 'finished',kickoff_at=when,
                    ft_home=None if i>=10000 else 2,ft_away=None if i>=10000 else 1,
                    created_at=NOW-timedelta(days=1000),updated_at=NOW-timedelta(days=1),raw={'source':'x'*1024}))
            for offset in range(0,len(records),500):
                await session.execute(insert(Match),records[offset:offset+500])
            ids=(await session.scalars(select(Match.id).where(Match.status=='finished').limit(3000))).all()
            for offset in range(0,len(ids),500):
                await session.execute(insert(MatchStatistics),[dict(match_id=id,is_final=True,
                    home_corners=4,away_corners=5,home_yellow_cards=2,away_yellow_cards=3,
                    home_red_cards=0,away_red_cards=0,collection_status='available',
                    updated_at=NOW-timedelta(days=1),raw={'source':'x'*1024}) for id in ids[offset:offset+500]])
            job=ScraperJob(league_id=league,kind='stats_backfill',status='running',discovery_complete=True,total_matches=1)
            session.add(job);await session.flush()
            item=JobItem(job_id=job.id,match_id=history[0],status='running');session.add(item)
            await session.commit();job_id=job.id
        monkeypatch.setenv('BETAPP_PROCESS_ROLE','scraper')
        scraper_db=Database(os.environ['TEST_POSTGRES_URL'])
        monkeypatch.delenv('BETAPP_PROCESS_ROLE')
        scraper_db.engine=scraper_db.engine.execution_options(schema_translate_map={None:schema})
        scraper_db.sessions=async_sessionmaker(scraper_db.engine,expire_on_commit=False)
        scraper_db.ready=True
        monkeypatch.setattr(routes,'database',db)
        monkeypatch.setattr(routes,'utcnow',lambda:NOW)
        monkeypatch.setattr(worker,'database',scraper_db)
        entered=asyncio.Event()
        async def fetch(client,id,lid):
            assert scraper_db.engine.pool.checkedout()==0, 'Network fetch must not hold a DB connection'
            entered.set();await release.wait()
            data=deepcopy(captured());data['raw']['match_id']=id
            return data
        monkeypatch.setattr(worker,'fetch_statistics',fetch)
        task=asyncio.create_task(worker.run_job(job_id))
        await asyncio.wait_for(entered.wait(),5)
        paths=['/health','/api/status','/api/diagnostics','/api/leagues','/api/matches?limit=50',
               '/api/predictions?limit=6','/api/predictions/best?limit=20','/api/market-performance']
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            async def request(path):
                started=time.perf_counter();response=await client.get(path)
                elapsed=time.perf_counter()-started
                assert response.status_code==200,(path,response.text)
                assert elapsed<12,(path,elapsed)
                return path,round(elapsed,3)
            timings=await asyncio.gather(*[request(path) for path in paths])
            assert (await client.get('/api/status')).json()['total_matches']==10161
            assert len((await client.get('/api/leagues')).json())==8
            print('PostgreSQL concurrent API seconds:',dict(timings))
            print('PostgreSQL warm board seconds:',dict(await asyncio.gather(*[request(paths[5]),request(paths[6])])))
        release.set();await asyncio.wait_for(task,15)
        async with db.session() as session:
            assert (await session.get(ScraperJob,job_id)).processed_matches==1
            assert (await session.get(ScraperJob,job_id)).status=='completed'
        with pytest.raises(TimeoutError):
            async with db.session() as session:
                (await session.get(ScraperJob,job_id)).processed_matches=999
                await session.flush()
                async with asyncio.timeout(.05):
                    await session.execute(text('SELECT pg_sleep(10)'))
        async with db.session() as session:
            assert await session.scalar(text('SELECT 1'))==1
            assert (await session.get(ScraperJob,job_id)).processed_matches==1
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        if scraper_db:await scraper_db.close()
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
