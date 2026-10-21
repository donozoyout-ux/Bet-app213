"""Durability and change-only publication contracts, no production credentials."""
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select,func,event
from src.models import BoardEdition,BoardHead,PredictionBoard,DailyCouponPublication,SavedCoupon,Match
from src.analytics.publications import publish_daily,read_daily,revisions,publish_board
from src.analytics.board_cache import refresh_one
from src.db import Database
from tests.test_predictions import seed,NOW
from tests.test_daily_pool import pool_fixture


async def test_same_model_data_keeps_version_generation_and_order(db):
    _,league,*_=await seed(db)
    async with db.session() as session:
        first=await session.get(BoardHead,league)
        version=first.version
        board=await session.get(PredictionBoard,league)
        generated=board.generated_at;order=[i['match']['id'] for i in board.payload['items']]
    await refresh_one(db,now=NOW+timedelta(minutes=1),force_league=league)
    async with db.session() as session:
        assert (await session.get(BoardHead,league)).version==version
        assert await session.scalar(select(func.count()).select_from(BoardEdition))==1
        board=await session.get(PredictionBoard,league)
        assert board.generated_at==generated
        assert [i['match']['id'] for i in board.payload['items']]==order
        data=deepcopy(board.payload);data['items'][0]['recommendations'][0]['probability']-=.001
        board.payload=data
        assert await publish_board(session,board,NOW+timedelta(minutes=2))!=version
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(BoardEdition))==2


async def test_daily_read_restart_same_inputs_and_expiry(db,monkeypatch):
    await pool_fixture(db,6)
    async with db.session() as session:
        data=await publish_daily(session,NOW);await session.commit()
        assert 4<len(data['items'])<=6  # Complete dynamic portfolio, no display cap.
        identity=data['id']
        first=await revisions(session,NOW)
    other=Database(str(db.engine.url))
    try:
        await other.initialize()
        async with other.session() as session:
            assert (await read_daily(session,NOW))['id']==identity
            assert await session.scalar(select(func.count()).select_from(DailyCouponPublication))==1
        import src.analytics.saved_coupons as saved
        async def forbidden(*args,**kwargs):raise AssertionError('Unchanged input re-evaluated')
        monkeypatch.setattr(saved,'automatic',forbidden)
        async with other.session() as session:
            assert (await publish_daily(session,NOW))['id']==identity
            assert await revisions(session,NOW)==first
            await session.commit()
        async with other.session() as session:
            assert (await read_daily(session,NOW+timedelta(minutes=16)))['items']==[]
            assert (await read_daily(session,NOW+timedelta(days=1)))['reason']=='not_published'
            assert await session.scalar(select(func.count()).select_from(SavedCoupon))==1
    finally:await other.close()


async def test_f5_daily_get_is_readonly_and_no_source_or_model_call(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await pool_fixture(db,4)
    assert (await api.get('/api/coupons/daily')).json()['reason']=='not_published'
    data=(await api.post('/api/coupons/automatic')).json()
    assert data['items']
    for _ in range(3):
        result=await api.get('/api/coupons/daily')
        assert result.status_code==200 and result.json()['id']==data['id']
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(SavedCoupon))==1
        match=await session.get(Match,data['items'][0]['match']['id']);match.status='live'
        await session.commit()
    assert (await api.get('/api/coupons/daily')).json()['items']==[]


async def test_revision_is_small_stable_and_changes_on_publication(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await pool_fixture(db,4)
    first=(await api.get('/api/dashboard/revision')).json()
    second=await api.get('/api/dashboard/revision')
    assert second.json()==first and len(second.content)<4000
    await api.post('/api/coupons/automatic')
    third=(await api.get('/api/dashboard/revision')).json()
    assert third['predictions']==first['predictions']
    assert third['coupons']!=first['coupons']


@pytest.mark.parametrize('width',[320,390,768,1024,1536])
async def test_revision_poll_f5_keeps_dom_and_never_creates_coupon(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright,expect
    from tests.test_minimal_analysis import fixtures,proxy
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await fixtures(db,4)
    async with db.session() as session:
        editions_before=await session.scalar(select(func.count()).select_from(BoardEdition))
    calls=[];tokens=None
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':900})
        await page.clock.install(time=NOW)
        async def forward(route):
            calls.append((route.request.method,route.request.url))
            if route.request.url.endswith('/api/dashboard/revision') and tokens:
                await route.fulfill(json=tokens);return
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions')
            await expect(page.locator('[data-analysis-match]')).to_have_count(4)
            await page.locator('[data-analysis-match]').nth(1).click()
            await expect(page.locator('[data-analysis-block=form]')).to_be_visible()
            await page.locator('[data-analysis-block=form]>summary').click()
            await page.wait_for_load_state('networkidle')
            await page.evaluate('window.keptCard=document.querySelectorAll("[data-analysis-match]")[1]')
            count=sum('/api/predictions' in path for method,path in calls)
            await page.clock.run_for(31000)
            await page.wait_for_load_state('networkidle')
            assert sum('/api/predictions' in path for method,path in calls)==count
            assert await page.evaluate('keptCard===document.querySelectorAll("[data-analysis-match]")[1]')
            await expect(page.locator('[data-analysis-block=form]')).to_have_attribute('open','')
            assert not any(method=='POST' and '/api/coupons/automatic' in path for method,path in calls)
            assert not any('/api/predictions/best' in path for method,path in calls)
            tokens=(await api.get('/api/dashboard/revision')).json()
            tokens['predictions']='changed-source-test'
            async with page.expect_request(lambda r:'/api/predictions?limit=8' in r.url):
                await page.evaluate('BetAppRevision.check()')
            await expect(page.locator('#match-analysis-status')).to_contain_text('Değişiklik yok')
            assert sum('/api/predictions?' in path for method,path in calls)==count+1
            assert await page.evaluate('keptCard===document.querySelectorAll("[data-analysis-match]")[1]')
            for b in tokens['boards'].values():
                b['expires_at']=(NOW+timedelta(minutes=20)).isoformat()
            await page.evaluate('BetAppRevision.check()')
            before=sum('/api/predictions?' in path for method,path in calls)
            await page.clock.fast_forward(600000)
            await expect(page.locator('[data-analysis-match]')).to_have_count(4)
            assert sum('/api/predictions?' in path for method,path in calls)==before

            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.reload()
            await expect(page.locator('[data-analysis-match]')).to_have_count(4)
            async with db.session() as session:
                assert await session.scalar(select(func.count()).select_from(BoardEdition))==editions_before
                assert await session.scalar(select(func.count()).select_from(SavedCoupon))==0
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_new_day_archives_and_same_prices_reuse_saved_coupon(db):
    from src.models import MarketOdds
    from tests.odds_fixture import move_observations
    await pool_fixture(db,4)
    async with db.session() as session:
        first=await publish_daily(session,NOW);await session.commit()
        snapshot=deepcopy((await session.get(SavedCoupon,first['id'])).payload)
        for row in await session.scalars(select(MarketOdds)):
            move_observations(row,NOW+timedelta(minutes=1))
        await session.commit()
        second=await publish_daily(session,NOW+timedelta(minutes=1));await session.commit()
        assert second['id']==first['id']
        assert (await session.get(SavedCoupon,first['id'])).payload==snapshot
        assert second['items'][0]['quote']['observed_at']!=first['items'][0]['quote']['observed_at']
        next_day=await publish_daily(session,NOW+timedelta(days=1));await session.commit()
        assert next_day['items']==[] and next_day['coupon_date']!=first['coupon_date']
        assert await session.scalar(select(func.count()).select_from(DailyCouponPublication))==2
        assert await session.scalar(select(func.count()).select_from(SavedCoupon))==1


async def test_one_expired_league_cannot_erase_other_fresh_predictions(api,db,monkeypatch):
    from src.models import League
    from src.analytics.board_cache import MODEL_VERSION
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await seed(db)
    async with db.session() as session:
        league=League(external_id=987765,name='Delayed league',country='Test',enabled=True)
        session.add(league);await session.flush()
        session.add(PredictionBoard(league_id=league.id,model_version=MODEL_VERSION,
            generated_at=NOW-timedelta(minutes=20),expires_at=NOW-timedelta(minutes=10),
            source_revision='old',payload=dict(items=[],availability=[],engine_analysis=[],coupon_records=[],evaluated=0)))
        await session.commit()
    for path in ['/api/predictions?limit=8','/api/predictions/best?statistical_all=true']:
        result=(await api.get(path)).json()
        assert result['items']
        assert result['cache']['stale'] is False and result['cache']['status']=='partial'
        assert result['cache']['stale_leagues']


async def test_coupon_504_preserves_only_unexpired_verified_publication(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await pool_fixture(db,4)
    assert (await api.post('/api/coupons/automatic')).status_code==200
    failed=False
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        await page.clock.install(time=NOW)
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/coupons/daily') and failed:
                await route.fulfill(status=504,json={'detail':'isolated timeout'});return
            response=await api.request(route.request.method,path,content=route.request.post_data,
                headers={k:v for k,v in route.request.headers.items() if k=='content-type'})
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#coupon')
            await expect(page.locator('.daily-selection')).to_have_count(4)
            failed=True
            await page.evaluate('BetAppCoupon.refresh()')
            await expect(page.locator('.daily-selection')).to_have_count(4)
            await expect(page.locator('#coupon-note')).to_contain_text('API hatası')
            await page.clock.fast_forward(901000)
            await expect(page.locator('.daily-selection')).to_have_count(0)
            assert 'Günlük kombine oran' not in await page.locator('#coupon-daily').inner_text()
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_revision_detects_correction_below_global_max_stamp(db):
    from src.models import MarketOdds
    await pool_fixture(db,4)
    async with db.session() as session:
        before=await revisions(session,NOW)
        row=await session.scalar(select(MarketOdds).order_by(MarketOdds.id))
        row.updated_at=NOW-timedelta(minutes=2)
        await session.commit()
        after=await revisions(session,NOW)
        assert after['predictions']!=before['predictions']


async def test_new_source_triggers_worker_before_ttl_expires(db):
    _,league,_,_,history=await seed(db)
    async with db.session() as session:
        first=(await session.get(PredictionBoard,league)).source_revision
        match=await session.get(Match,history[0]);match.ft_home+=1
        match.updated_at=NOW+timedelta(seconds=30)
        await session.commit()
    assert await refresh_one(db,now=NOW+timedelta(minutes=1)) is True
    async with db.session() as session:
        assert (await session.get(PredictionBoard,league)).source_revision!=first


async def test_same_content_metadata_does_not_create_an_edition(db):
    _,league,*_=await seed(db)
    async with db.session() as session:
        board=await session.get(PredictionBoard,league)
        first=(await session.get(BoardHead,league)).version
        data=deepcopy(board.payload)
        data['items'][0]['match']['updated_at']=(NOW+timedelta(seconds=30)).isoformat()
        board.payload=data
        assert await publish_board(session,board,NOW+timedelta(minutes=1))==first
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(BoardEdition))==1


async def test_unchanged_worker_does_not_rewrite_large_payload(db):
    _,league,*_=await seed(db)
    queries=[]
    def trace(conn,cursor,statement,*args):queries.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:
        await refresh_one(db,now=NOW+timedelta(minutes=1),force_league=league)
        updates=[q for q in queries if q.startswith('UPDATE prediction_boards')]
        assert updates and all('payload' not in q for q in updates)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',trace)


async def test_stage_profile_logs_counts_without_sql_or_bind_values(api,db,monkeypatch,caplog):
    import logging,json
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await seed(db)
    caplog.set_level(logging.INFO,logger='src.query_profile');caplog.clear()
    assert (await api.get('/api/predictions?limit=8')).status_code==200
    assert (await api.get('/api/coupons/daily')).status_code==200
    messages=[r.getMessage() for r in caplog.records if r.name=='src.query_profile']
    assert any('stage=board_read' in m for m in messages)
    assert any('stage=current_quotes' in m for m in messages)
    assert any('stage=daily_read' in m and 'sql_count=' in m for m in messages)
    assert not any('SELECT ' in m or 'Pool team' in m or 'TEST_SECRET' in m for m in messages)
    print('PUBLICATION_PHASES '+json.dumps(messages))
