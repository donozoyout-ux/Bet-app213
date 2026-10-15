"""No real Telegram calls: stored fixtures and injected HTTP transport only."""
from copy import deepcopy
from datetime import timedelta,datetime,timezone
import asyncio
import logging
import os
import uuid

import httpx
import pytest
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema,DropSchema
from src.db import Database
from src.models import SavedCoupon,TelegramDelivery,PredictionBoard,MarketOdds,Match,CollectorRetry
from src.analytics.saved_coupons import automatic,save_manual,detail
from src.analytics.coupon import DraftRequest
from src.jobs.telegram import dispatch,prepare,window,message,status
from tests.test_coupon import coupon_seed as base_coupon_seed,request,NOW
from src.analytics.saved_coupons import daily_candidates
from tests.odds_fixture import observed_prices, move_observations

async def coupon_seed(db):
    ids=await base_coupon_seed(db)
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard))
        data=deepcopy(board.payload)
        for item in data['items']:
            match=await session.get(Match,item['match']['id'])
            match.kickoff_at=NOW+timedelta(hours=5)
            item['match']['kickoff_at']=(NOW+timedelta(hours=5)).isoformat()
        board.payload=data
        await session.commit()
    return ids

OWNER='12345678-1234-1234-1234-123456789012'
TOKEN='12345:TEST_SECRET_ONLY'
CHAT='-100999999'


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN);monkeypatch.setenv('TELEGRAM_CHAT_ID',CHAT)


async def many_matches(db,count=12):
    ids=await coupon_seed(db)
    async with db.session() as session:
        first=await session.get(Match,ids[0]);board=await session.get(PredictionBoard,first.league_id)
        items=deepcopy(board.payload['items']);template=items[0]
        existing=(await session.scalars(select(MarketOdds).where(MarketOdds.match_id==first.id))).all()
        for index in range(count-2):
            match=Match(external_match_id=770000+index,league_id=first.league_id,season_id=first.season_id,round=3,
                home_team_id=first.home_team_id,away_team_id=first.away_team_id,kickoff_at=NOW+timedelta(hours=5),status='scheduled',raw={},created_at=NOW,updated_at=NOW)
            session.add(match);await session.flush()
            item=deepcopy(template);item['match']['id']=match.id;item['match_id']=match.id;items.append(item)
            for odds in existing:
                session.add(MarketOdds(match_id=match.id,bookmaker_id=odds.bookmaker_id,market=odds.market,period=odds.period,latest_line=odds.latest_line,latest_over=odds.latest_over,latest_under=odds.latest_under,updated_at=NOW-timedelta(minutes=1),raw=observed_prices(NOW-timedelta(minutes=1),'latest_over','latest_under')))
        board.payload={**board.payload,'items':items,'evaluated':len(items)};await session.commit()
    return ids


async def test_automatic_unlimited_strong_exact_quotes_and_empty(db):
    await many_matches(db)
    async with db.session() as session:
        coupon=await automatic(session,NOW);await session.commit()
        assert len(coupon.payload['items'])==4
        assert len({i['match']['id'] for i in coupon.payload['items']})==4
        assert coupon.payload['combined_odds']==pytest.approx(2.2**4,abs=1e-6)
        assert all(i['quote']['bookmaker'] in {'Crown','Bet365','Sbobet'} for i in coupon.payload['items'])
        assert (await automatic(session,NOW)).id==coupon.id
        for row in await session.scalars(select(MarketOdds)):move_observations(row,NOW-timedelta(hours=1))
        await session.commit();assert await automatic(session,NOW) is None
        for row in await session.scalars(select(MarketOdds)):move_observations(row,NOW)
        board=await session.scalar(select(PredictionBoard))
        data=deepcopy(board.payload)
        for item in data['items']:
            for rec in item['recommendations']:rec['qualifies']=False
        board.payload=data;await session.commit();assert await automatic(session,NOW) is None
        for item in data['items']:
            for rec in item['recommendations']:rec['qualifies']=True;rec['probability']=.1
        board.payload=deepcopy(data);await session.commit();assert await automatic(session,NOW) is None


async def test_versioned_manual_edits_do_not_change_automatic(db):
    ids=await coupon_seed(db)
    async with db.session() as session:
        auto=await automatic(session,NOW);original=deepcopy(auto.payload);await session.commit()
        manual=await save_manual(session,DraftRequest(**request(ids)),OWNER,NOW);await session.commit()
        unchanged=await save_manual(session,DraftRequest(**request(ids)),OWNER,NOW,base_id=manual.id)
        assert unchanged.id==manual.id
        edited=await save_manual(session,DraftRequest(**request(ids[:1])),OWNER,NOW,base_id=manual.id);await session.commit()
        assert edited.root_id==manual.root_id and edited.revision==2
        assert len(edited.payload['items'])==1 and len(manual.payload['items'])==2
        assert auto.payload==original and (await automatic(session,NOW)).id==auto.id
        with pytest.raises(Exception) as exc:await save_manual(session,DraftRequest(**request(ids[:1])),OWNER,NOW,base_id=manual.id)
        assert exc.value.status_code==409
        match=await session.get(Match,ids[0]);match.status='finished';match.ft_home=2;match.ft_away=1;match.ht_home=1;match.ht_away=1
        await session.commit()
        result=await detail(session,edited)
        assert result['items'][0]['match']['score']=='2–1' and result['items'][0]['status']=='won'


@pytest.mark.parametrize('hour,minute,expected',[(8,59,None),(9,0,12),(14,59,12),(15,0,18),(20,59,18),(21,0,None)])
def test_istanbul_daily_windows(hour,minute,expected):
    now=datetime(2026,10,8,hour,minute,tzinfo=timezone.utc)
    slot=window(now)
    assert (datetime.fromisoformat(slot).hour if slot else None)==expected


async def freshen(db,now):
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):move_observations(row,now-timedelta(minutes=1))
        for row in await session.scalars(select(PredictionBoard)):row.generated_at=now;row.expires_at=now+timedelta(minutes=10)
        await session.commit()


async def verify_scheduling(db):
    await coupon_seed(db);calls=[]
    def telegram(req):
        calls.append(req);return httpx.Response(200,json={'ok':True,'result':{'message_id':len(calls)}})
    transport=httpx.MockTransport(telegram)
    noon=NOW.replace(hour=9);await freshen(db,noon)
    first=await dispatch(db,noon,transport=transport);assert first['status']=='sent'
    # New transactions see the committed ledger, independent of in-memory timers.
    again=await dispatch(db,noon+timedelta(minutes=5),transport=transport)
    assert again['status']=='sent' and len(calls)==1
    evening=NOW.replace(hour=15);await freshen(db,evening)
    # A refreshed timestamp alone must not resend the noon selections.
    assert (await dispatch(db,evening,transport=transport))['status']=='duplicate_selections'
    assert len(calls)==1
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard));data=deepcopy(board.payload)
        for item in data['items']:
            for rec in item['recommendations']:
                if rec['market']=='corners':rec['qualifies']=False
        board.payload=data
        retry=await session.get(CollectorRetry,'telegram:'+window(evening))
        retry.next_attempt_at=evening
        await session.commit()
    assert (await dispatch(db,evening,transport=transport))['status']=='sent'
    assert (await dispatch(db,evening+timedelta(hours=1),transport=transport))['status']=='sent'
    assert len(calls)==2
    async with db.session() as session:
        rows=(await session.scalars(select(TelegramDelivery))).all()
        assert len(rows)==2 and len({r.slot for r in rows})==2
        safe=await status(session,evening)
        assert TOKEN not in str(safe) and CHAT not in str(safe)
        assert safe['automatic_daily_limit']==2


async def test_schedule_both_slots_and_restart_ledger(db,configured):
    await verify_scheduling(db)


async def test_simultaneous_senders_and_same_version_manual(db,configured):
    await coupon_seed(db)
    async with db.session() as session:
        auto=await automatic(session,NOW);await session.commit();id=auto.id
    calls=[]
    async def send(req):calls.append(req);await asyncio.sleep(.05);return httpx.Response(200,json={'ok':True,'result':{'message_id':5}})
    results=await asyncio.gather(dispatch(db,NOW,coupon_id=id,transport=httpx.MockTransport(send)),dispatch(db,NOW,coupon_id=id,transport=httpx.MockTransport(send)))
    assert len(calls)==1 and any(r['status']=='sent' for r in results)
    assert (await dispatch(db,NOW,coupon_id=id,transport=httpx.MockTransport(send)))['status']=='duplicate_version'


@pytest.mark.parametrize('mode,expected',[('timeout','uncertain'),('http500','uncertain'),('malformed','uncertain'),('reject','failed')])
async def test_telegram_failure_no_blind_retry(db,configured,mode,expected,caplog):
    await coupon_seed(db);calls=[]
    def send(req):
        calls.append(req)
        if mode=='timeout':raise httpx.ReadTimeout('contains '+TOKEN,request=req)
        if mode=='http500':return httpx.Response(500,json={'ok':False})
        if mode=='malformed':return httpx.Response(200,text='bad response '+TOKEN)
        return httpx.Response(403,json={'ok':False,'description':'secret '+TOKEN})
    with caplog.at_level(logging.INFO):
        result=await dispatch(db,NOW,transport=httpx.MockTransport(send))
        assert result['status']==expected
        await dispatch(db,NOW+timedelta(minutes=1),transport=httpx.MockTransport(send))
    assert len(calls)==1 and TOKEN not in caplog.text and TOKEN not in str(result)
    async with db.session() as session:
        row=await session.scalar(select(TelegramDelivery));assert row.status==expected and row.error_code


async def test_interrupted_sender_not_retried(db,configured):
    await coupon_seed(db)
    claim=await prepare(db,NOW);assert claim['status']=='claimed'
    result=await dispatch(db,NOW+timedelta(minutes=3),transport=httpx.MockTransport(lambda _:pytest.fail('must not send')))
    assert result['status']=='uncertain'


async def test_missing_settings_and_empty_dont_send(db,monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN',raising=False);monkeypatch.delenv('TELEGRAM_CHAT_ID',raising=False)
    assert (await dispatch(db,NOW))['status']=='disabled'
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN);monkeypatch.setenv('TELEGRAM_CHAT_ID',CHAT)
    result=await dispatch(db,NOW,transport=httpx.MockTransport(lambda _:pytest.fail('empty coupon')))
    assert result['status']=='no_eligible_selections'
    async with db.session() as session:assert await session.scalar(select(func.count(TelegramDelivery.id)))==0


async def test_message_format_and_document_for_large_coupon(db,configured):
    await many_matches(db,45);calls=[]
    def send(req):calls.append(req);return httpx.Response(200,json={'ok':True,'result':{'message_id':4}})
    result=await dispatch(db,NOW,transport=httpx.MockTransport(send));assert result['status']=='sent'
    assert calls[0].url.path.endswith('/sendMessage')
    content=calls[0].content.decode()
    assert 'Toplam' in content and ('Bet365' in content or 'Crown' in content)
    assert 'Oluşturulma' in content and 'Toplam' in content and 'garantisi yok' in content


async def test_saved_api_private_owner_and_manual_admin_guard(api,db,monkeypatch,configured):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids=await coupon_seed(db)
    auto=(await api.post('/api/coupons/automatic')).json();assert len(auto['items'])==2
    data=(await api.post('/api/coupons',json=request(ids),headers={'X-Coupon-Key':OWNER})).json()
    assert (await api.get('/api/coupons/'+data['id'])).status_code==404
    assert (await api.get('/api/coupons/'+data['id'],headers={'X-Coupon-Key':OWNER})).status_code==200
    assert 'owner_hash' not in data
    assert (await api.post('/api/coupons/'+data['id']+'/telegram')).status_code in (401,503)
    safe=(await api.get('/api/telegram/status')).json()
    assert safe['configured'] and TOKEN not in str(safe) and CHAT not in str(safe)


async def test_chromium_automatic_edit_save_history_and_results(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await many_matches(db,4)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':1280,'height':900})
        await page.clock.install(time=NOW)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key','authorization')}
            response=await api.request(route.request.method,route.request.url.removeprefix('http://test'),content=route.request.post_data,headers=headers)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward);await page.goto('http://test/#coupon')
        # Opening the page populates the automatic coupon without a generate click.
        await expect(page.locator('#coupon-selected button')).to_have_count(4)
        await expect(page.locator('#coupon-saved-note')).to_contain_text('Otomatik')
        await expect(page.locator('#coupon-daily')).to_be_visible()
        assert not await page.locator('#coupon-edit-panel').evaluate('(el)=>el.open')
        await page.locator('#coupon-tools > summary').click()
        await page.locator('#coupon-selected button').first.click()
        await expect(page.locator('#coupon-selected button')).to_have_count(3)
        await expect(page.locator('#coupon-telegram')).to_be_disabled()
        # Add a different qualified prediction for the removed fixture.
        await page.locator('#coupon-edit-panel summary').click()
        available=page.locator('[data-coupon-pick]:enabled:not(:checked)')
        await available.last.check()
        await expect(page.locator('#coupon-selected button')).to_have_count(4)
        await page.locator('#coupon-save').click()
        await expect(page.locator('#coupon-note')).to_contain_text('PostgreSQL')
        await expect(page.locator('#coupon-saved-note')).to_contain_text('Düzenlenmiş')
        await page.locator('#coupon-selected button').first.click()
        await page.locator('#coupon-save').click()
        await expect(page.locator('#coupon-saved-note')).to_contain_text('Sürüm 2')
        await expect(page.locator('#coupon-history button')).to_have_count(3)
        await page.locator('#coupon-automatic').click()
        await expect(page.locator('#coupon-selected button')).to_have_count(4)
        await page.locator('#coupon-manual').click()
        await expect(page.locator('#coupon-selected button')).to_have_count(3)
        await page.reload()
        await expect(page.locator('#coupon-selected button')).to_have_count(3)
        await expect(page.locator('#coupon-mode')).to_contain_text('Manuel')
        await expect(page.locator('#coupon-daily .daily-selection')).to_have_count(4)
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert errors==[]
        await page.unroute_all(behavior='ignoreErrors');await browser.close()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_concurrency_restart_and_immutable_versions(configured):
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='telegram_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await verify_scheduling(db)
        restarted=Database(os.environ['TEST_POSTGRES_URL'])
        restarted.engine=restarted.engine.execution_options(schema_translate_map={None:schema});restarted.sessions=async_sessionmaker(restarted.engine,expire_on_commit=False)
        try:
            result=await dispatch(restarted,NOW.replace(hour=15)+timedelta(minutes=5),transport=httpx.MockTransport(lambda _:pytest.fail('restart duplicate')))
            assert result['status']=='sent'
        finally:await restarted.close()
        # Concurrent fresh manual claims on PostgreSQL are serialized without HTTP transactions.
        async with db.session() as session:
            old=await session.scalar(select(SavedCoupon).join(TelegramDelivery,TelegramDelivery.coupon_id==SavedCoupon.id).where(TelegramDelivery.status=='sent').order_by(TelegramDelivery.started_at.desc()).limit(1));copy=SavedCoupon(id=str(uuid.uuid4()),root_id=str(uuid.uuid4()),revision=1,kind='manual',owner_hash=None,created_at=NOW,payload=old.payload);session.add(copy);await session.commit();id=copy.id
        await freshen(db,NOW)
        calls=[]
        async def send(req):calls.append(req);await asyncio.sleep(.05);return httpx.Response(200,json={'ok':True,'result':{'message_id':7}})
        results=await asyncio.gather(*[dispatch(db,NOW,coupon_id=id,transport=httpx.MockTransport(send)) for _ in range(4)])
        assert len(calls)==1,results
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


def test_daily_balanced_four_and_no_forced_filler():
    def entry(id,kickoff,score=.8,prob=.7,price=1.8):
        return {'match':{'id':id,'kickoff_at':kickoff},'recommendation':{'score':score,'reliability':score,'probability':prob},'quote':{'price':price}}
    today=(NOW+timedelta(hours=5)).isoformat()
    tomorrow=(NOW+timedelta(days=1)).isoformat()
    # All ranking fixtures meet the strict gate; weak records are tested separately.
    records=[entry(i,today,score=.80+i*.01) for i in range(6)]
    records.append(entry(99,tomorrow,score=.99))
    picks=daily_candidates(records,NOW)
    assert len(picks)==4
    assert {x['match']['id'] for x in picks}=={2,3,4,5}
    assert all(x['match']['id']!=99 for x in picks)
    assert len(daily_candidates(records[:2],NOW))==2
    assert daily_candidates([entry(7,tomorrow)],NOW)==[]
    assert len(daily_candidates(records,NOW,strategy='cautious',max_matches=3))==3
    assert len(daily_candidates(records,NOW,strategy='value',max_matches=3))==3
    assert daily_candidates([entry(7,today,score=.79)],NOW)==[]
    assert daily_candidates([entry(7,today,prob=.69)],NOW)==[]
    assert daily_candidates([entry(7,today,score=float('nan'))],NOW)==[]
    assert daily_candidates([entry(7,today,price=1)],NOW)==[]


async def test_strategy_considers_both_verified_alternatives_and_keeps_metadata(db):
    await many_matches(db,4)
    async with db.session() as session:
        balanced=await automatic(session,NOW)
        cautious=await automatic(session,NOW,strategy='cautious',max_matches=4)
        two=await automatic(session,NOW,strategy='cautious',max_matches=2)
        assert balanced.payload['combined_odds']==pytest.approx(2.2**4)
        assert cautious.payload['combined_odds']==pytest.approx(1.8**4)
        assert two.payload['target_matches']==2
        assert two.payload['strategy']=='cautious'
        assert len(two.payload['items'])==2
        assert (await automatic(session,NOW,strategy='cautious',max_matches=2)).id==two.id


@pytest.mark.parametrize('field,value',[('probability',.699),('reliability',.799),('score',.799)])
async def test_manual_cannot_bypass_automatic_quality_floor(api,db,monkeypatch,field,value):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids=await coupon_seed(db)
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard));data=deepcopy(board.payload)
        for item in data['items']:
            for rec in item['recommendations']:rec[field]=value
        board.payload=data;await session.commit()
    quote=(await api.post('/api/coupon/quote',json=request(ids))).json()
    assert not quote['eligible'] and quote['combined_odds'] is None
    assert (await api.post('/api/coupons',json=request(ids),headers={'X-Coupon-Key':OWNER})).status_code==409
    assert (await api.post('/api/coupons/automatic')).json()['items']==[]


@pytest.mark.parametrize('body',[[],None,{'ok':True,'result':None},{'ok':True,'result':{'message_id':True}}])
async def test_malformed_telegram_success_is_uncertain_without_retry(db,configured,body):
    await coupon_seed(db);calls=[]
    def send(req):calls.append(req);return httpx.Response(200,json=body)
    result=await dispatch(db,NOW,transport=httpx.MockTransport(send))
    assert result['status']=='uncertain'
    await dispatch(db,NOW+timedelta(minutes=1),transport=httpx.MockTransport(send))
    assert len(calls)==1


@pytest.mark.parametrize('token,chat,state',[('',CHAT,'missing'),(TOKEN,'','missing'),('invalid',CHAT,'invalid'),(TOKEN,'123','configured'),(TOKEN,CHAT,'configured')])
async def test_safe_configuration_status(db,monkeypatch,token,chat,state):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',token);monkeypatch.setenv('TELEGRAM_CHAT_ID',chat)
    async with db.session() as session:data=await status(session,NOW)
    assert data['configuration_status']==state
    assert data['configured']==(state=='configured')
    assert data['connection_verified'] is False
    assert TOKEN not in str(data) and CHAT not in str(data)


def test_telegram_400_safe_reason_classification():
    from src.jobs.telegram import telegram_client_error
    assert telegram_client_error(400, 'Bad Request: chat not found') == 'telegram_chat_not_found'
    assert telegram_client_error(400, 'Bad Request: not enough rights to send text messages') == 'telegram_missing_permissions'
    assert telegram_client_error(400, 'Bad Request: message is too long') == 'telegram_message_too_long'
    assert telegram_client_error(400, 'Bad Request: chat was upgraded') == 'telegram_chat_migrated'
    assert telegram_client_error(403, 'Forbidden: bot was kicked from the supergroup') == 'telegram_bot_forbidden'
    assert telegram_client_error(400, 'sensitive token and private chat details') == 'telegram_400'
