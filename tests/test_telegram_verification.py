"""Bot identity and group permissions through read-only MockTransport calls."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import logging

import httpx
import pytest
from sqlalchemy import func, select
from src.models import TelegramDelivery
import src.jobs.telegram_verification as verification
from tests.test_automatic_telegram import TOKEN, CHAT, coupon_seed, freshen
from tests.test_coupon import NOW

BOT_ID = 7777777
USER = {'id': BOT_ID, 'is_bot': True, 'username': 'BKupon8Bot'}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', CHAT)
    monkeypatch.setattr(verification, '_cached', None)
    monkeypatch.setattr(verification, '_lock', asyncio.Lock())
    monkeypatch.setattr(verification, 'utcnow', lambda: NOW)


def provider(calls, member=None, chat=None, me=None, error=None):
    async def response(request):
        method = request.url.path.rsplit('/', 1)[-1]
        calls.append(method)
        assert method in ('getMe', 'getChat', 'getChatMember')
        assert request.method == 'POST' and not request.url.query
        params = json.loads(request.content)
        if error and method == error[0]:
            return httpx.Response(error[1], json=error[2])
        if method == 'getMe':
            result = USER if me is None else me
        elif method == 'getChat':
            assert params == {'chat_id': CHAT}
            result = chat or {'id': int(CHAT), 'type': 'supergroup',
                'permissions': {'can_send_messages': True, 'can_send_documents': True}}
        else:
            assert params == {'chat_id': CHAT, 'user_id': BOT_ID}
            result = {'user': USER, **(member or {'status': 'administrator'})}
        return httpx.Response(200, json={'ok': True, 'result': result})
    return httpx.MockTransport(response)


@pytest.mark.parametrize('member', [
    {'status': 'administrator'}, {'status': 'creator'}, {'status': 'member'},
    {'status': 'restricted', 'is_member': True, 'can_send_messages': True, 'can_send_documents': True},
])
async def test_read_only_identity_membership_and_permissions(configured, member, caplog):
    calls = []
    with caplog.at_level(logging.DEBUG):
        result = await verification.verify(transport=provider(calls, member=member))
    assert result['connection_verified'] and result['status'] == 'verified'
    assert result['can_send_messages'] and result['can_send_documents']
    assert calls == ['getMe', 'getChat', 'getChatMember']
    assert TOKEN not in caplog.text and CHAT not in caplog.text
    assert TOKEN not in str(result) and CHAT not in str(result) and str(BOT_ID) not in str(result)


@pytest.mark.parametrize('member,state', [
    ({'status': 'left'}, 'bot_removed'), ({'status': 'kicked'}, 'bot_removed'),
    ({'status': 'restricted', 'is_member': False}, 'bot_removed'),
    ({'status': 'restricted', 'is_member': True, 'can_send_messages': False, 'can_send_documents': True}, 'missing_permissions'),
    ({'status': 'restricted', 'is_member': True, 'can_send_messages': True, 'can_send_documents': False}, 'missing_permissions'),
    ({'status': 'restricted', 'is_member': True}, 'unknown_permissions'),
])
async def test_denied_or_unknown_permissions_never_claim_success(configured, member, state):
    calls = []
    result = await verification.verify(transport=provider(calls, member=member))
    assert result['status'] == state and not result['connection_verified']
    assert calls == ['getMe', 'getChat', 'getChatMember']


async def test_member_requires_explicit_chat_permissions(configured):
    result = await verification.verify(transport=provider([], member={'status': 'member'},
        chat={'id': int(CHAT), 'type': 'supergroup'}))
    assert result['status'] == 'unknown_permissions'


async def test_wrong_bot_stops_before_group_lookup(configured):
    calls = []
    result = await verification.verify(transport=provider(calls, me={**USER, 'username': 'OtherBot'}))
    assert result['status'] == 'wrong_bot' and calls == ['getMe']


@pytest.mark.parametrize('code,description,state', [
    (400, 'Bad Request: chat not found', 'telegram_chat_not_found'),
    (400, 'not enough rights', 'telegram_missing_permissions'),
    (400, 'unknown response', 'telegram_400'),
    (403, 'Forbidden', 'telegram_bot_forbidden'),
    (429, 'Too Many Requests', 'telegram_429'),
])
async def test_provider_reasons_are_allowlisted_and_secret_free(configured, code, description, state, caplog):
    calls = []
    error = ('getChat', code, {'ok': False, 'description': description+' '+TOKEN+' '+CHAT})
    with caplog.at_level(logging.DEBUG):
        result = await verification.verify(transport=provider(calls, error=error))
    assert result['status'] == state and calls == ['getMe', 'getChat']
    assert TOKEN not in str(result) and CHAT not in str(result)
    assert TOKEN not in caplog.text and CHAT not in caplog.text


@pytest.mark.parametrize('mode', ['timeout', 'malformed', 'http500', 'redirect'])
async def test_unavailable_read_checks_do_not_retry_or_send(configured, mode):
    calls = []
    def response(req):
        calls.append(req)
        if mode == 'timeout':
            raise httpx.ReadTimeout(TOKEN+' '+CHAT, request=req)
        if mode == 'http500':
            return httpx.Response(500, json={'ok': False})
        if mode == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://example.invalid/'})
        return httpx.Response(200, json={'ok': True, 'result': []})
    result = await verification.verify(transport=httpx.MockTransport(response))
    assert result['status'] == 'unavailable' and len(calls) == 1
    assert TOKEN not in str(result) and CHAT not in str(result)


async def test_cache_concurrency_expiry_and_changed_credentials(configured, monkeypatch):
    calls = []; transport = provider(calls)
    results = await asyncio.gather(*[verification.verify(transport=transport) for _ in range(6)])
    assert all(r['status'] == 'verified' for r in results) and len(calls) == 3
    assert verification.snapshot(NOW+timedelta(minutes=6))['status'] == 'expired'
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '-100111111')
    assert verification.snapshot()['status'] == 'not_checked'
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '')
    assert (await verification.verify(transport=httpx.MockTransport(lambda _: pytest.fail('missing credentials'))))['status'] == 'missing'


async def test_authenticated_verify_api_and_public_safe_status(api, db, configured, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    monkeypatch.setattr(routes, 'settings', replace(routes.settings, scraper_token='management-test-only'))
    calls = []; transport = provider(calls); real_client = httpx.AsyncClient
    monkeypatch.setattr(verification.httpx, 'AsyncClient', lambda **kw: real_client(**{**kw, 'transport': transport}))
    assert (await api.post('/api/telegram/verify')).status_code == 401
    result = await api.post('/api/telegram/verify', headers={'Authorization': 'Bearer management-test-only'})
    assert result.status_code == 200 and result.json()['connection_verified']
    safe = (await api.get('/api/telegram/status')).json()
    assert safe['connection_verified'] and safe['connection']['read_only']
    assert TOKEN not in str(safe) and CHAT not in str(safe)
    async with db.session() as session:
        assert await session.scalar(select(func.count(TelegramDelivery.id))) == 0
    assert calls == ['getMe', 'getChat', 'getChatMember']


async def test_verification_and_daily_slots_remain_independent_idempotent(db, configured):
    from src.jobs.telegram import dispatch, window
    await coupon_seed(db)
    checks = []
    assert (await verification.verify(transport=provider(checks)))['connection_verified']
    sends = []
    def send(req):
        sends.append(req)
        assert req.url.path.endswith('/sendMessage')
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(sends)}})
    noon = NOW.replace(hour=9); await freshen(db, noon)
    for _ in range(3):
        assert (await dispatch(db, noon, transport=httpx.MockTransport(send)))['status'] == 'sent'
    evening = NOW.replace(hour=15); await freshen(db, evening)
    assert (await dispatch(db, evening, transport=httpx.MockTransport(send)))['status'] == 'duplicate_selections'
    assert len(sends) == 1 and len(checks) == 3
    async with db.session() as session:
        row = await session.scalar(select(TelegramDelivery))
        assert row.slot == window(noon)


async def test_credentials_changed_during_check_cannot_reuse_old_success(configured, monkeypatch):
    calls=[]; original=provider(calls)
    async def response(req):
        result=await original.handle_async_request(req)
        if req.url.path.endswith('/getChatMember'):
            monkeypatch.setenv('TELEGRAM_CHAT_ID','-100111111')
        return result
    result=await verification.verify(transport=httpx.MockTransport(response))
    assert result['status']=='not_checked' and not result['connection_verified']


async def test_chromium_read_only_verify_button_and_safe_help(api, db, configured, monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    monkeypatch.setattr(routes,'settings',replace(routes.settings,scraper_token='management-test-only'))
    calls=[]; transport=provider(calls); real_client=httpx.AsyncClient
    monkeypatch.setattr(verification.httpx,'AsyncClient',lambda **kw:real_client(**{**kw,'transport':transport}))
    async with async_playwright() as p:
        browser=await p.chromium.launch(); page=await browser.new_page()
        await page.clock.install(time=NOW)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            headers={k:v for k,v in route.request.headers.items() if k in ('authorization','content-type','x-coupon-key')}
            response=await api.request(route.request.method,route.request.url.removeprefix('http://test'),content=route.request.post_data,headers=headers)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#coupon')
            await page.locator('#coupon-tools > summary').click()
            await page.get_by_text('Telegram Yönetimi ve Manuel Paylaşım',exact=True).click()
            await page.locator('#telegram-verify').click()
            await expect(page.locator('#telegram-check-note')).to_contain_text('yönetim erişim anahtarı')
            assert calls==[]
            await page.locator('#telegram-admin-token').fill('management-test-only')
            await page.locator('#telegram-verify').click()
            await expect(page.locator('#telegram-check-note')).to_contain_text('@BKupon8Bot')
            await expect(page.locator('#telegram-check-note')).to_contain_text('Test mesajı gönderilmedi')
            assert calls==['getMe','getChat','getChatMember']
            assert TOKEN not in await page.locator('body').inner_text()
            assert CHAT not in await page.locator('body').inner_text()
            assert 'management-test-only' not in await page.evaluate('JSON.stringify({...localStorage})')
            assert errors==[]
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
    async with db.session() as session:
        assert await session.scalar(select(func.count(TelegramDelivery.id)))==0


async def test_private_chat_read_only_success(configured, monkeypatch):
    from src.jobs.telegram import configuration
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123456789')
    assert configuration()[2]
    calls = []
    async def response(request):
        method = request.url.path.rsplit('/', 1)[-1]
        calls.append(method)
        if method == 'getMe':
            return httpx.Response(200, json={'ok': True, 'result': USER})
        assert method == 'getChat'
        assert json.loads(request.content) == {'chat_id': '123456789'}
        return httpx.Response(200, json={'ok': True, 'result': {'id': 123456789, 'type': 'private'}})
    result = await verification.verify(transport=httpx.MockTransport(response))
    assert result['connection_verified'] and result['status'] == 'verified'
    assert calls == ['getMe', 'getChat']


async def test_private_chat_mismatched_id_rejected(configured, monkeypatch):
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123456789')
    async def response(request):
        method = request.url.path.rsplit('/', 1)[-1]
        result = USER if method == 'getMe' else {'id': 987654321, 'type': 'private'}
        return httpx.Response(200, json={'ok': True, 'result': result})
    result = await verification.verify(transport=httpx.MockTransport(response))
    assert result['status'] == 'unsupported_chat' and not result['connection_verified']


async def test_one_shot_test_message_success_without_coupon_delivery(api, db, configured, monkeypatch):
    from src.jobs.telegram import send_test_message
    calls = []
    async def mock_send(request):
        calls.append(request)
        assert request.url.path.endswith('/sendMessage')
        payload = json.loads(request.content)
        assert payload['chat_id'] == CHAT
        assert 'test' in payload['text'].lower()
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 23}})
    result = await send_test_message(httpx.MockTransport(mock_send))
    assert result == {'status': 'sent', 'message_id': 23}
    assert len(calls) == 1
    async with db.session() as session:
        assert await session.scalar(select(func.count(TelegramDelivery.id))) == 0
    assert (await api.post('/api/telegram/test-message')).status_code in (401, 503)


async def test_one_shot_test_message_rejected_and_transport_ambiguous(configured):
    from src.jobs.telegram import send_test_message
    result = await send_test_message(httpx.MockTransport(
        lambda req: httpx.Response(403, json={'ok': False, 'description': 'Forbidden: bot was blocked by the user'})))
    assert result['status'] == 'failed' and 'error_code' in result
    def timeout(req):
        raise httpx.ReadTimeout('timeout', request=req)
    result = await send_test_message(httpx.MockTransport(timeout))
    assert result['status'] == 'uncertain'

@pytest.mark.parametrize('with_admin',[False,True])
async def test_late_status_preserves_explicit_verification_feedback(api,db,configured,monkeypatch,with_admin):
    """A stale startup GET must not replace a local warning or a completed check."""
    import asyncio
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'settings',replace(routes.settings,scraper_token='management-test-only'))
    calls=[];transport=provider(calls);real_client=httpx.AsyncClient
    monkeypatch.setattr(verification.httpx,'AsyncClient',lambda **kw:real_client(**{**kw,'transport':transport}))
    entered=asyncio.Event();release=asyncio.Event()
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            headers={k:v for k,v in route.request.headers.items() if k in ('authorization','content-type','x-coupon-key')}
            response=await api.request(route.request.method,path,content=route.request.post_data,headers=headers)
            if path=='/api/telegram/status':
                entered.set();await release.wait()
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#coupon',wait_until='domcontentloaded')
            await page.locator('#coupon-tools > summary').click()
            await asyncio.wait_for(entered.wait(),10)
            await page.get_by_text('Telegram Yönetimi ve Manuel Paylaşım',exact=True).click()
            if with_admin:await page.locator('#telegram-admin-token').fill('management-test-only')
            await page.locator('#telegram-verify').click()
            expected='@BKupon8Bot' if with_admin else 'yönetim erişim anahtarı'
            await expect(page.locator('#telegram-check-note')).to_contain_text(expected)
            release.set()
            await page.wait_for_load_state('networkidle')
            await expect(page.locator('#telegram-check-note')).to_contain_text(expected)
            assert calls==(['getMe','getChat','getChatMember'] if with_admin else [])
        finally:
            release.set();await page.unroute_all(behavior='ignoreErrors');await browser.close()
    async with db.session() as session:
        assert await session.scalar(select(func.count(TelegramDelivery.id)))==0
