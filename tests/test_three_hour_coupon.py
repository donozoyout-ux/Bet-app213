"""Synthetic stored fixtures; every Telegram request uses MockTransport."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import asyncio
import os
import uuid
import httpx
import pytest
from sqlalchemy import select,func,event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema,DropSchema
from src.db import Database
from src.models import SavedCoupon,TelegramDelivery,DailyCouponPublication,MarketOdds,PredictionBoard,CollectorRetry
from src.jobs.telegram import window,dispatch,prepare,status,TZ
from src.analytics.publications import publish_daily,read_daily
from tests.test_automatic_telegram import coupon_seed,freshen,configured,NOW
from tests.test_daily_pool import pool_fixture
from tests.odds_fixture import move_observations

@pytest.mark.parametrize('hour',range(0,24,3))
def test_all_eight_local_checkpoints_and_late_wake(hour):
    local=datetime(2026,10,21,hour,tzinfo=TZ)
    assert datetime.fromisoformat(window(local.astimezone(timezone.utc)))==local
    assert datetime.fromisoformat(window(local+timedelta(hours=2,minutes=59)))==local
    assert datetime.fromisoformat(window(local+timedelta(hours=3)))==local+timedelta(hours=3)

async def test_full_publication_without_four_cap_and_no_observation_only_versions(db):
    await pool_fixture(db,6)
    async with db.session() as session:
        first=await publish_daily(session,NOW,force=True);await session.commit()
        assert len(first['items'])>4 and first['status']=='published'
    await freshen(db,NOW+timedelta(minutes=1))
    async with db.session() as session:
        second=await publish_daily(session,NOW+timedelta(minutes=1),force=True);await session.commit()
        assert second['id']==first['id']
        assert await session.scalar(select(func.count()).select_from(SavedCoupon))==1
        assert (await read_daily(session,NOW+timedelta(days=1)))['items']==[]

async def test_same_picks_changed_price_no_send_but_changed_selection_sends(db,configured):
    await coupon_seed(db);calls=[]
    transport=httpx.MockTransport(lambda request:(calls.append(request) or httpx.Response(200,json={'ok':True,'result':{'message_id':len(calls)}})))
    assert (await dispatch(db,NOW,transport=transport))['status']=='sent'
    later=NOW+timedelta(hours=3);await freshen(db,later)
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):
            row.latest_under+=.01;row.latest_over+=.01
        await session.commit()
    assert (await dispatch(db,later,transport=transport))['status']=='duplicate_selections'
    assert len(calls)==1
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard));data=deepcopy(board.payload)
        for item in data['items']:
            for rec in item['recommendations']:
                if rec['market']=='corners':rec['qualifies']=False
        board.payload=data;await session.commit()
    later+=timedelta(minutes=5);await freshen(db,later)
    assert (await dispatch(db,later,transport=transport))['status']=='sent'
    assert len(calls)==2
    body=calls[-1].content.decode()
    assert 'Kuponanalizii' in body and '2 seçim' in body and 'Kalite' in body
    async with db.session() as session:
        publication=await read_daily(session,later)
        delivery=await session.scalar(select(TelegramDelivery).where(TelegramDelivery.slot==window(later)))
        assert delivery.coupon_id==publication['id']

@pytest.mark.parametrize('age,expected',[(899,'claimed'),(900,'no_eligible_selections'),(901,'no_eligible_selections')])
async def test_price_expiry_is_not_relaxed(db,configured,age,expected):
    await coupon_seed(db)
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):move_observations(row,NOW-timedelta(seconds=age))
        await session.commit()
    assert (await prepare(db,NOW))['status']==expected

async def test_quote_race_has_durable_retry_and_no_delivery(db,configured,monkeypatch):
    import src.jobs.telegram as telegram
    await coupon_seed(db)
    async def changed(*args,**kwargs):return dict(eligible=False,items=[])
    monkeypatch.setattr(telegram,'quote_draft',changed)
    assert (await prepare(db,NOW))['status']=='quotes_changed'
    async with db.session() as session:
        retry=await session.get(CollectorRetry,'telegram:'+window(NOW))
        assert retry.reason=='quotes_changed'
        assert await session.scalar(select(func.count()).select_from(TelegramDelivery))==0
    assert (await prepare(db,NOW+timedelta(minutes=1)))['status']=='waiting_for_quotes'

async def test_current_diagnostics_is_readonly_and_explains_unpriced_quality(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await coupon_seed(db)
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):move_observations(row,NOW-timedelta(seconds=901))
        await session.commit()
    statements=[]
    def collect(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',collect)
    try:
        result=await api.get('/api/coupons/diagnostics');assert result.status_code==200
        data=result.json();assert data['read_only'] and data['quality_matches']==2 and data['eligible_matches']==0
        assert data['price_rejections'] and data['publication']['selections']==0
        assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',collect)

async def test_wake_does_not_replay_missed_slots_or_yesterdays_coupon(db,configured):
    await coupon_seed(db)
    now=NOW+timedelta(days=1,hours=1)
    result=await dispatch(db,now,transport=httpx.MockTransport(lambda _:pytest.fail('old coupon')))
    assert result['status']=='no_eligible_selections'
    async with db.session() as session:
        info=await status(session,now)
        assert len(info['times'])==8 and info['wake_policy']=='current_slot_only'
        assert len(info['today'])==8
        assert await session.scalar(select(func.count()).select_from(TelegramDelivery))==0

@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_concurrent_automatic_generation_and_delivery(configured):
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='three_hour_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await coupon_seed(db);calls=[]
        async def send(request):
            calls.append(request);await asyncio.sleep(.05)
            return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        results=await asyncio.gather(*[dispatch(db,NOW,transport=httpx.MockTransport(send)) for _ in range(4)])
        assert len(calls)==1,results
        async with db.session() as session:
            for model in (SavedCoupon,DailyCouponPublication,TelegramDelivery):
                assert await session.scalar(select(func.count()).select_from(model))==1
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


@pytest.mark.parametrize('width',[390,1536])
async def test_coupon_diagnostics_and_three_hour_schedule_browser(api,db,monkeypatch,width):
    from pathlib import Path
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await pool_fixture(db,6)
    published=(await api.post('/api/coupons/automatic')).json()
    assert len(published['items'])>4
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960})
        await page.clock.install(time=NOW);calls=[];errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            assert route.request.url.startswith('http://test/')
            path=route.request.url.removeprefix('http://test');calls.append(path)
            assert route.request.method=='GET' or path in ('/api/coupon/status','/api/coupon/quote'), (route.request.method,path)
            response=await api.request(route.request.method,path,content=route.request.post_data,headers={'Content-Type':'application/json'})
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#coupon')
            await expect(page.locator('.daily-selection')).to_have_count(len(published['items']))
            await page.get_by_text('Neden kupon yok? Güncel salt okunur tanı',exact=True).click()
            await page.get_by_role('button',name='Aday ve fiyat durumunu kontrol et').click()
            await expect(page.locator('#coupon-daily')).to_contain_text('İncelenen 20')
            assert '/api/coupons/diagnostics' in calls
            await page.locator('#coupon-tools > summary').click()
            await page.get_by_text('Telegram Yönetimi ve Manuel Paylaşım',exact=True).click()
            await expect(page.locator('#telegram-schedule')).to_contain_text('00/03/06/09/12/15/18/21')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=str(target/f'coupon-{width}.png'),full_page=True)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
