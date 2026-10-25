"""Synthetic lifecycle and isolated pool contention; no production or sends."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import time
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import Depends
from sqlalchemy import select, func, text, event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from src.db import Database, AdmissionTimeout
from src.models import (Match, Team, Season, League, PredictionSnapshot,
    PredictionSettlement, MatchStatistics, WorkerCheckpoint)
from src.jobs.settlement import sweep, record_failure, health
from src.analytics.settlements import correct_settlement

NOW=datetime(2026,10,25,12,tzinfo=timezone.utc)


async def captured_match(db):
    async with db.session() as s:
        league=await s.scalar(select(League.id).limit(1))
        season=Season(league_id=league,season_name='2026-2027')
        teams=[Team(external_id=71000+i,name=f'Synthetic settlement {i}') for i in range(2)]
        s.add_all([season,*teams]);await s.flush()
        m=Match(external_match_id=71888,league_id=league,season_id=season.id,round=1,
            home_team_id=teams[0].id,away_team_id=teams[1].id,kickoff_at=NOW-timedelta(hours=4),
            status='scheduled',raw={},created_at=NOW-timedelta(days=1))
        s.add(m);await s.flush()
        picks=[dict(id=market,market=market,selection=selection,probability=.7,reliability=.8,
            score=.8,sample_size=40,label=market,confidence='medium') for market,selection in (
                ('result','home'),('goals','over_2_5'),('corners','over_7_5'))]
        s.add(PredictionSnapshot(match_id=m.id,league_id=league,captured_at=NOW-timedelta(hours=5),
            model_version='synthetic-before-kickoff',picks=picks))
        await s.commit()
        return m.id, picks


async def test_full_lifecycle_restart_delayed_statistics_corrections_and_counts(api,db,monkeypatch):
    id,picks=await captured_match(db)
    from src.jobs.storage import store_live
    async with db.session() as s:
        m=await s.get(Match,id)
        await store_live(s,m,dict(status='live',live_minute='80',ft_home=2,ft_away=1,raw={}),NOW-timedelta(hours=1))
        await s.commit()
    assert await sweep(db,NOW)
    async with db.session() as s:
        assert await s.scalar(select(func.count(PredictionSettlement.id)))==0
        # An authoritative final scoreboard can arrive through a bulk import as
        # well as store_live. The independent sweep repairs missed event hooks.
        m=await s.get(Match,id);m.status='finished';m.ft_home=2;m.ft_away=1;m.status_observed_at=NOW
        await s.commit()
    assert await sweep(db,NOW+timedelta(seconds=61))
    async with db.session() as s:
        rows=list(await s.scalars(select(PredictionSettlement)))
        assert len(rows)==2 and {r.pick_index for r in rows}=={0,1}
        assert all(r.revision==1 and r.recorded_at for r in rows)
        assert (await s.get(PredictionSnapshot,id)).picks==picks
    # Resume the durable cursor from a new engine; missing corner totals remain
    # awaiting-data, never losses. First wrap, then process delayed final stats.
    restarted=Database(str(db.engine.url),role='settlement')
    try:
        assert await sweep(restarted,NOW+timedelta(seconds=122))
        async with db.session() as s:
            s.add(MatchStatistics(match_id=id,is_final=True,collection_status='available',
                home_corners=4,away_corners=5,updated_at=NOW,raw={}))
            await s.commit()
        assert await sweep(restarted,NOW+timedelta(seconds=183))
        assert not await sweep(restarted,NOW+timedelta(seconds=184))
    finally:await restarted.close()
    async with db.session() as s:
        assert await s.scalar(select(func.count(PredictionSettlement.id)))==3
        row=await s.get(WorkerCheckpoint,'settlement')
        assert row.failures==0 and row.last_success_at and row.written==1
        m=await s.get(Match,id);m.ft_home=0;m.ft_away=1;await s.flush()
        for index in (0,1):
            await correct_settlement(s,id,index,expected_revision=1,actor='test-reviewer',reason='Verified synthetic correction')
        await s.commit()
    from src.analytics.learning import refresh
    await refresh(db)
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(days=1))
    chart=(await api.get('/api/performance-trends')).json()
    assert chart['daily'][-1]['decided']==3 and chart['daily'][-1]['won']==1 and chart['daily'][-1]['lost']==2
    assert (await api.get('/api/market-performance')).json()['summary']['settled']==3
    assert (await api.get('/api/chart-diagnostics')).json()['outcomes']['settled']==3
    assert (await api.get('/api/learning/summary')).json()['summary']['predictions']==3
    async with db.session() as s:
        assert (await s.get(PredictionSnapshot,id)).picks==picks
        assert await s.scalar(select(func.count(PredictionSettlement.id)))==5  # 3 originals + 2 revisions


async def test_sweep_failure_rolls_back_cursor_preserves_success_and_retries(db,monkeypatch):
    await captured_match(db)
    assert await sweep(db,NOW)
    import src.jobs.settlement as module
    original=module.settle_batch
    async def fail(session,**kwargs):
        await original(session,**kwargs)
        raise RuntimeError('private test value must never be persisted')
    monkeypatch.setattr(module,'settle_batch',fail)
    with pytest.raises(RuntimeError):await sweep(db,NOW+timedelta(seconds=61))
    await record_failure(db,RuntimeError('SECRET'),NOW+timedelta(seconds=61))
    async with db.session() as s:
        status=await health(s,NOW+timedelta(seconds=62))
        assert status['heartbeat']=='retrying' and status['error_type']=='RuntimeError'
        assert status['last_success_at']==NOW and status['cursor']==0
        assert 'SECRET' not in json.dumps(status,default=str)
    monkeypatch.setattr(module,'settle_batch',original)
    assert not await sweep(db,NOW+timedelta(seconds=63))
    assert await sweep(db,NOW+timedelta(seconds=72))
    async with db.session() as s:assert (await health(s,NOW+timedelta(seconds=73)))['heartbeat']=='ok'


async def test_browser_moves_from_pending_timeline_to_real_worker_outcomes(api,db,monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(days=1))
    id,_=await captured_match(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':1280,'height':960})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            assert route.request.method=='GET'
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/')
            await expect(page.locator('#home-results-settled')).to_have_text('0')
            await expect(page.locator('#home-results-chart')).to_contain_text('0 doğrulanmış sonuç')
            assert await sweep(db,NOW)
            async with db.session() as s:
                m=await s.get(Match,id);m.status='finished';m.ft_home=2;m.ft_away=1;m.status_observed_at=NOW
                await s.commit()
            assert await asyncio.create_task(sweep(db,NOW+timedelta(seconds=61)))
            # Refresh is a GET consumer; no page writes or retrospective captures.
            await page.evaluate('BetAppResults.refresh()')
            await expect(page.locator('#home-results-settled')).to_have_text('2')
            await expect(page.locator('#home-results-won')).to_have_text('2')
            await expect(page.locator('#home-results-chart path[data-series]')).to_have_count(3)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_settlement_batches_do_not_read_raw_or_query_decisions_per_match(db):
    id,_=await captured_match(db)
    async with db.session() as s:
        m=await s.get(Match,id);m.status='finished';m.ft_home=2;m.ft_away=1;await s.commit()
    statements=[]
    def trace(conn,cursor,sql,params,ctx,many):statements.append(sql)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:
        from src.analytics.settlements import settle_batch
        async with db.session() as s:
            await settle_batch(s);await s.commit()
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',trace)
    selects=[sql for sql in statements if sql.startswith('SELECT')]
    assert len(selects)==2 and all('matches.raw' not in sql for sql in selects)
    assert sum(sql.startswith('INSERT') for sql in statements)==1


async def test_market_worker_releases_transaction_before_both_provider_calls(db,monkeypatch):
    from src.models import ScraperJob, JobItem, MarketOdds
    import src.jobs.worker as worker
    id,_=await captured_match(db)
    async with db.session() as s:
        m=await s.get(Match,id)
        job=ScraperJob(league_id=m.league_id,kind='market_odds',status='running',discovery_complete=True,total_matches=1)
        s.add(job);await s.flush()
        item=JobItem(job_id=job.id,match_id=id,status='pending');s.add(item);await s.commit()
        job_id,item_id=job.id,item.id
    monkeypatch.setattr(worker,'database',db)
    observed=[]
    async def half(*args,**kwargs):
        observed.append(db.engine.pool.checkedout())
        return {'Crown':{'ou':dict(latest_line=1.5,latest_over=1.8,latest_under=2.0,raw={})}}
    async def corners(*args,**kwargs):
        observed.append(db.engine.pool.checkedout())
        return {'Crown':dict(latest_line=8.5,latest_over=1.8,latest_under=2.0,raw={})}
    monkeypatch.setattr(worker,'fetch_halftime_odds',half)
    monkeypatch.setattr(worker,'fetch_corner_odds',corners)
    await worker.process_item(job_id,item_id,object())
    assert observed==[0,0]
    async with db.session() as s:
        assert (await s.get(JobItem,item_id)).status=='completed'
        assert await s.scalar(select(func.count(MarketOdds.id)))==2


async def test_pool_measurements_return_to_zero_on_cancel(db):
    entered=asyncio.Event()
    async def waiting():
        async with db.session() as s:
            await s.execute(text('SELECT 1'));entered.set();await asyncio.Event().wait()
    task=asyncio.create_task(waiting());await entered.wait()
    assert db.pool_health()['checked_out']==1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert db.pool_health()['checked_out']==0 and db.pool_health()['high_water']>=1


async def test_role_pool_budget_does_not_grow():
    roles={}
    for role in ('web','scraper','learning','settlement','ai'):
        db=Database('postgresql://test:placeholder@localhost/test',role=role)
        try:roles[role]=db.engine.pool.size()
        finally:await db.close()
    assert roles==dict(web=5,scraper=2,learning=1,settlement=1,ai=1)
    assert roles['web']+roles['scraper']+roles['learning']+roles['settlement']==9  # Previously 5+2+2.


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='isolated PostgreSQL required')
async def test_postgres_five_connection_starvation_reserved_admission_and_slow_clients(monkeypatch):
    from src.api.main import app
    from src.api.routes import session_dependency
    import src.api.routes as routes
    db=Database(os.environ['TEST_POSTGRES_URL'],role='web')
    schema='pool134_'+uuid.uuid4().hex
    release=asyncio.Event();entered=0;pressure=[];clients=[]
    original_routes=list(app.router.routes)
    async def pressure_endpoint(session=Depends(session_dependency,scope='function')):
        nonlocal entered
        await session.execute(text('SELECT 1'));entered+=1
        await release.wait()
        return {'ok':True}
    app.add_api_route('/api/_pool134',pressure_endpoint)
    try:
        async with db.engine.begin() as c:await c.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        monkeypatch.setattr(routes,'database',db)
        original=db.request_slot
        @asynccontextmanager
        async def bypass(lane):yield lambda:None
        paths=['/api/leagues','/api/bookmakers','/api/coupons/daily','/api/dashboard/revision']
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            monkeypatch.setattr(db,'request_slot',bypass)
            pressure=[asyncio.create_task(client.get('/api/_pool134')) for _ in range(5)]
            async with asyncio.timeout(5):
                while entered<5:await asyncio.sleep(.01)
            assert db.engine.pool.checkedout()==5
            started=time.perf_counter()
            before=await asyncio.gather(*[client.get(path) for path in paths])
            before_time=time.perf_counter()-started
            assert all(r.status_code==503 for r in before)
            release.set();await asyncio.gather(*pressure)
            monkeypatch.setattr(db,'request_slot',original)
            release=asyncio.Event();entered=0
            pressure=[asyncio.create_task(client.get('/api/_pool134')) for _ in range(5)]
            async with asyncio.timeout(5):
                while entered<3:await asyncio.sleep(.01)
            assert db.engine.pool.checkedout()==3
            started=time.perf_counter()
            after=await asyncio.gather(*[client.get(path) for path in paths])
            after_time=time.perf_counter()-started
            assert all(r.status_code==200 for r in after),[r.text for r in after]
            assert after_time<1.5
            release.set();await asyncio.gather(*pressure)
            print(json.dumps(dict(before_status=[r.status_code for r in before],after_status=[r.status_code for r in after],
                before_ms=round(before_time*1000,1),after_ms=round(after_time*1000,1),pool_size=db.engine.pool.size())))
        # Five slow clients must no longer own five SQL connections or admission
        # tokens after response serialization completes.
        release=asyncio.Event();bodies=0
        from src.api.read_cache import catalog_cache
        catalog_cache.databases.pop(db.engine,None)
        previous_checkouts=db.pool_health()['checkouts']
        async def slow_send_app(scope,receive,send):
            async def blocked(message):
                nonlocal bodies
                if message['type']=='http.response.body':bodies+=1;await release.wait()
                await send(message)
            await app(scope,receive,blocked)
        async with AsyncClient(transport=ASGITransport(app=slow_send_app),base_url='http://test') as client:
            clients=[asyncio.create_task(client.get('/api/leagues')) for _ in range(5)]
            async with asyncio.timeout(5):
                while bodies<5:await asyncio.sleep(.01)
            assert db.engine.pool.checkedout()==0
            assert db.pool_health()['checkouts']>previous_checkouts
            release.set();assert all(r.status_code==200 for r in await asyncio.gather(*clients))
        assert db.pool_health()['checked_out']==0
        id,_=await captured_match(db)
        async with db.session() as s:
            m=await s.get(Match,id);m.status='finished';m.ft_home=2;m.ft_away=1;await s.commit()
        results=await asyncio.gather(*[sweep(db,NOW) for _ in range(5)])
        assert sum(results)==1
        async with db.session() as s:
            assert await s.scalar(select(func.count(PredictionSettlement.id)))==2
            assert (await s.get(WorkerCheckpoint,'settlement')).written==2
    finally:
        release.set()
        await asyncio.gather(*pressure,*clients,return_exceptions=True)
        app.router.routes[:]=original_routes
        async with db.engine.begin() as c:await c.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
