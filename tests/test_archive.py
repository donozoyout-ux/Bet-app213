"""Synthetic data only. No real exports, Telegram, NVIDIA, or provisioning."""
import base64
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
import httpx
import pytest
from src.archive.schema import validate,collect,canonical
from src.archive.crypto import seal,unseal,write_new
from src.archive.restore import restore_new,read_catalog
from src.archive.github import upload
from tests.test_performance_status import fixture

KEY=base64.urlsafe_b64encode(b'k'*32).decode()


def data():
    return dict(schema_version='kupon-owned-audit-v1',created_at='2026-10-24T00:00:00+00:00',rights_reference='APP-119',
        forecasts=[dict(match_id=1,captured_at='2026-10-08T12:00:00+00:00',model_digest='a'*64,picks=[dict(index=0,market='goals',selection='over_2_5',probability=.8)])],
        decisions=[dict(id=1,match_id=1,pick_index=0,revision=1,outcome='won',recorded_at='2026-10-09T12:00:00+00:00')],coupons=[],reports=[])


def test_encrypt_authenticate_restore_and_readonly_catalog(tmp_path):
    original=data();blob=seal(original,KEY)
    assert b'over_2_5' not in blob and b'APP-119' not in blob
    assert unseal(blob,KEY)==original
    restored=tmp_path/'audit.sqlite';restore_new(unseal(blob,KEY),restored)
    assert read_catalog(restored)==original
    with pytest.raises(FileExistsError):restore_new(original,restored)
    with sqlite3.connect(restored) as conn:
        with pytest.raises(sqlite3.DatabaseError):conn.execute("UPDATE archive_records SET identity='rewritten'")
    assert read_catalog(restored)==original


@pytest.mark.parametrize('field',['ciphertext','nonce','ciphertext_sha256','manifest'])
def test_corruption_and_manifest_tampering_rejected(field):
    envelope=json.loads(seal(data(),KEY))
    if field=='manifest':envelope[field]['counts']['forecasts']=100
    else:envelope[field]='AAAA'
    with pytest.raises(ValueError):unseal(canonical(envelope),KEY)
    with pytest.raises(ValueError):unseal(seal(data(),KEY),base64.urlsafe_b64encode(b'x'*32).decode())


@pytest.mark.parametrize('field,value',[('evidence',{'innocent':'SECRET'}),('note','SECRET'),('raw',{'safe':'SECRET'})])
def test_allowlist_rejects_disguised_secrets(field,value):
    original=data();original['forecasts'][0]['picks'][0][field]=value
    with pytest.raises(ValueError):validate(original)


def test_free_text_and_orphan_decision_rejected():
    original=data();original['forecasts'][0]['picks'][0]['selection']='SECRET_TOKEN'
    with pytest.raises(ValueError):validate(original)
    original=data();original['decisions'][0]['match_id']=999
    with pytest.raises(ValueError):validate(original)
    original=data();original['decisions']*=2
    with pytest.raises(ValueError):validate(original)


async def test_export_queries_allowlisted_columns_and_no_provider_content(db):
    from sqlalchemy import event
    from src.models import PredictionSnapshot
    ident=await fixture(db)
    async with db.session() as session:
        # Fixture creates immutable records; export never mutates them.
        before=(await session.get(PredictionSnapshot,ident)).picks
    queries=[]
    def capture(conn,cursor,statement,parameters,context,executemany):queries.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:
        async with db.engine.connect() as conn:exported=await collect(conn,'APP-119')
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert len(queries)==4 and all(q.lstrip().upper().startswith('SELECT') for q in queries)
    assert not any(word in ' '.join(queries) for word in ('owner_hash','payload','evidence','reason','source_manifest'))
    assert ident in {r['match_id'] for r in exported['forecasts']}
    assert len(exported['decisions'])==4
    assert all(set(p)=={'index','market','selection','probability'} for f in exported['forecasts'] for p in f['picks'])
    async with db.session() as session:assert (await session.get(PredictionSnapshot,ident)).picks==before


async def test_github_public_repo_refused_before_upload_and_private_encrypted_only():
    requests=[]
    def respond(request):
        requests.append(request)
        if request.method=='GET':return httpx.Response(200,json={'private':False})
        raise AssertionError('Public upload attempted')
    with pytest.raises(ValueError):await upload('test/archive','test-token',seal(data(),KEY),KEY,transport=httpx.MockTransport(respond))
    assert len(requests)==1
    requests.clear()
    def private(request):
        requests.append(request)
        if request.method=='GET':return httpx.Response(200,json={'private':True})
        if request.url.host=='uploads.github.com':
            assert unseal(request.content,KEY)==data()
            return httpx.Response(201,json={'id':2})
        if request.method=='POST':return httpx.Response(201,json={'id':1,'upload_url':'https://evil.invalid'})
        return httpx.Response(200,json={'id':1})
    await upload('test/archive','test-token',seal(data(),KEY),KEY,transport=httpx.MockTransport(private))
    assert len(requests)==4 and all(r.url.host in {'api.github.com','uploads.github.com'} for r in requests)


async def test_archive_api_no_database_and_tamper_failure_recovery(api,tmp_path,monkeypatch):
    path=tmp_path/'audit.enc.json';write_new(path,seal(data(),KEY))
    monkeypatch.setenv('ARCHIVE_FILE',str(path));monkeypatch.setenv('ARCHIVE_ENCRYPTION_KEY',KEY)
    assert (await api.get('/api/archive-summary')).status_code==503
    monkeypatch.setenv('ARCHIVE_PUBLIC_SUMMARY_APPROVED','true')
    result=(await api.get('/api/archive-summary')).json()
    assert result['live'] is False and result['recorded_won']==1 and result['mode']=='historical_archive'
    assert not {'forecasts','coupons','reports','success_rate','match_id'}&result.keys()
    path.write_bytes(b'corrupted')
    response=await api.get('/api/archive-summary')
    assert response.status_code==503 and KEY not in response.text
    path.write_bytes(seal(data(),KEY))
    assert (await api.get('/api/archive-summary')).status_code==200
    assert (await api.get('/archive')).status_code==200


async def test_explicit_archive_adapter_never_connects_or_accepts_writes(monkeypatch):
    import src.db as module
    monkeypatch.setattr(module,'settings',replace(module.settings,storage_backend='archive',app_env='production'))
    db=module.Database('postgresql://unused:unused@localhost/original')
    assert not db.configured
    await db.initialize()
    assert not db.ready
    with pytest.raises(RuntimeError):
        async with db.session():pass
    await db.close()


async def test_archive_browser_honest_mobile_fallback(api,tmp_path,monkeypatch):
    from playwright.async_api import async_playwright,expect
    path=tmp_path/'audit.enc.json';write_new(path,seal(data(),KEY))
    monkeypatch.setenv('ARCHIVE_FILE',str(path));monkeypatch.setenv('ARCHIVE_ENCRYPTION_KEY',KEY);monkeypatch.setenv('ARCHIVE_PUBLIC_SUMMARY_APPROVED','true')
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':390,'height':844})
        async def forward(route):
            r=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=r.status_code,body=r.content,content_type=r.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/archive')
            await expect(page.locator('#status')).to_contain_text('Canlı veri değil')
            await expect(page.locator('#metrics article')).to_have_count(5)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('ARCHIVE_SCREENSHOT_DIR'):
                target=Path(os.environ['ARCHIVE_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(target/'archive-390.png'),full_page=True)
        finally:await page.unroute_all(behavior='ignoreErrors');await browser.close()


def test_row_cap_and_schema_version_fail_closed(monkeypatch,tmp_path):
    import src.archive.schema as schema
    monkeypatch.setattr(schema,'MAX_ROWS',0)
    with pytest.raises(ValueError):seal(data(),KEY)
    original=data();original['schema_version']='future-version'
    with pytest.raises(ValueError):validate(original)
    target=tmp_path/'existing.enc.json';write_new(target,b'original')
    with pytest.raises(FileExistsError):write_new(target,b'replacement')
    assert target.read_bytes()==b'original'


async def test_archive_live_endpoints_stay_unavailable(api,monkeypatch):
    import src.api.routes as routes
    from src.api.main import app
    from src.api.routes import session_dependency
    from src.db import Database
    db=Database('')
    monkeypatch.setattr(routes,'database',db)
    previous=app.dependency_overrides.pop(session_dependency)
    try:
        response=await api.get('/api/matches')
        assert response.status_code==503 and 'Live data unavailable' in response.text
        assert (await api.get('/health')).status_code==200
    finally:app.dependency_overrides[session_dependency]=previous
