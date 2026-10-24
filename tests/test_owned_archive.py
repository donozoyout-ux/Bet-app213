"""Synthetic exports only. No production credentials, messages or provider calls."""
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid
import httpx
import pytest
from sqlalchemy import text, event
from src.archive.schema import SCHEMA, collect, validate, canonical
from src.archive.crypto import seal, unseal, write_new
from src.archive.restore import restore_new, read_catalog
from src.archive.cli import daily, verify_restore, configuration
from src.archive.github import ReleaseArchive, PREFIX, ASSET
from src.archive.source import source_engine, validate_reader
from src.archive.watchdog import check

KEY=base64.urlsafe_b64encode(b'k'*32).decode()
NOW=datetime(2026,10,24,3,35,tzinfo=timezone.utc)


def data(empty=False,day='2026-10-24'):
    return dict(schema_version=SCHEMA,created_at=day+'T03:35:00+00:00',rights_reference='APP-124',
        forecasts=[] if empty else [dict(match_id=1,captured_at='2026-10-20T12:00:00+00:00',model_digest='a'*64,
            picks=[dict(index=0,market='goals',selection='over_2_5',probability=.8)])],
        decisions=[] if empty else [dict(id=1,match_id=1,pick_index=0,revision=1,outcome='won',recorded_at='2026-10-21T12:00:00+00:00')],
        coupons=[] if empty else [dict(id='01234567-1234-1234-1234-0123456789ab',revision=1,created_at='2026-10-20T12:00:00+00:00')],
        reports=[] if empty else [dict(id=1,created_at='2026-10-21T12:00:00+00:00',method_digest='b'*64)],
        coverage=dict(day=day,leagues=[],database_bytes=100,quota_bytes=1000,previous_database_bytes=None,
                      growth_bytes=None,previous_day=None,missing_run_days=0,expected_league_codes=[],missing_league_codes=[]))


def env(tmp_path):
    return dict(ARCHIVE_ACTIVATED='true',ARCHIVE_DATABASE_URL='postgresql://synthetic@localhost/test?sslmode=verify-full',
        ARCHIVE_ENCRYPTION_KEY=KEY,ARCHIVE_GITHUB_TOKEN='synthetic-token',ARCHIVE_REPOSITORY='test/private',
        ARCHIVE_RIGHTS_REFERENCE='APP-124',ARCHIVE_BUDGET_BYTES='10000000',NEON_QUOTA_BYTES='1000',
        ARCHIVE_KEEP_DAYS='30',ARCHIVE_EXPECTED_LEAGUES='36,31',ARCHIVE_REPORT=str(tmp_path/'coverage.json'))


@pytest.mark.parametrize('empty',[False,True])
def test_ciphertext_catalog_roundtrip_and_no_overwrite(tmp_path,empty):
    original=data(empty);blob=seal(original,KEY)
    assert b'over_2_5' not in blob and b'APP-124' not in blob
    assert unseal(blob,KEY)==original
    verify_restore(original)
    target=tmp_path/'audit.sqlite';restore_new(original,target)
    assert read_catalog(target)==original
    with pytest.raises(FileExistsError):restore_new(original,target)
    with sqlite3.connect(target) as conn:
        with pytest.raises(sqlite3.DatabaseError):conn.execute("DELETE FROM archive_metadata")
    assert read_catalog(target)==original


@pytest.mark.parametrize('field',['ciphertext','nonce','manifest','ciphertext_sha256'])
def test_authenticated_corruption_rejected(field):
    envelope=json.loads(seal(data(),KEY))
    if field=='manifest':envelope[field]['counts']['forecasts']=999
    else:envelope[field]='AAAA'
    with pytest.raises(ValueError):unseal(canonical(envelope),KEY)
    with pytest.raises(ValueError):unseal(seal(data(),KEY),base64.urlsafe_b64encode(b'x'*32).decode())


@pytest.mark.parametrize('group,field,value',[
    ('forecasts','innocent','credential hidden under innocuous key'),
    ('coupons','payload',{'safe':'Goaloo raw odds'}),('reports','summary','private narrative'),
    ('decisions','evidence',{'score':2}),('coverage','secret','token')])
def test_allowlist_rejects_unknown_fields(group,field,value):
    original=data()
    target=original[group] if group=='coverage' else original[group][0]
    target[field]=value
    with pytest.raises(ValueError):validate(original)


def test_invalid_enum_or_duplicate_or_orphan_or_cap(monkeypatch):
    original=data();original['forecasts'][0]['picks'][0]['selection']='hidden secret'
    with pytest.raises(ValueError):validate(original)
    original=data();original['decisions']*=2
    with pytest.raises(ValueError):validate(original)
    original=data();original['decisions'][0]['match_id']=999
    with pytest.raises(ValueError):validate(original)
    import src.archive.schema as schema
    monkeypatch.setattr(schema,'MAX_ROWS',0)
    with pytest.raises(ValueError):validate(data())


def test_missing_configuration_and_tls_fail_before_network(tmp_path):
    for field in env(tmp_path):
        if field=='ARCHIVE_REPORT':continue
        values=env(tmp_path);values.pop(field)
        with pytest.raises((ValueError,KeyError)):configuration(values)
    for url in ('sqlite:///bad','postgresql://reader@host/db','postgresql://reader@host/db?sslmode=require'):
        with pytest.raises(ValueError):source_engine(url)


async def test_explicit_queries_drop_disguised_provider_payloads(db):
    from tests.test_performance_status import fixture
    from src.models import PredictionSnapshot, SavedCoupon, LearningReport, Match
    from sqlalchemy import select
    ident=await fixture(db)
    async with db.session() as session:
        match=await session.scalar(select(Match).outerjoin(PredictionSnapshot,PredictionSnapshot.match_id==Match.id).where(PredictionSnapshot.match_id.is_(None),Match.kickoff_at.is_not(None)).limit(1))
        session.add(PredictionSnapshot(match_id=match.id,league_id=match.league_id,captured_at=match.kickoff_at-timedelta(hours=1),
            model_version='synthetic',picks=[dict(market='goals',selection='over_2_5',probability=.8,safe_text='SECRET Goaloo 1.95',odds=1.95)]))
        session.add(SavedCoupon(id=str(uuid.uuid4()),root_id=str(uuid.uuid4()),revision=1,kind='manual',owner_hash='SECRET',payload={'note':'SECRET'},created_at=NOW))
        session.add(LearningReport(method_version='synthetic',data_fingerprint='a'*64,created_at=NOW,source_observed_at=NOW,source_manifest=[{'secret':'SECRET'}],summary={'note':'SECRET'},coverage={}))
        await session.commit()
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:
        async with db.engine.connect() as conn:result=await collect(conn,'APP-124',NOW)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    result['coverage']=data()['coverage'];validate(result)
    assert len(statements)==4 and all(s.lstrip().startswith('SELECT') for s in statements)
    assert 'SECRET' not in canonical(result).decode() and '1.95' not in canonical(result).decode()
    assert len(result['decisions'])==4 and len(result['coupons'])==len(result['reports'])==1
    assert not any(x in ' '.join(statements) for x in ('owner_hash','payload','evidence','source_manifest','summary'))


class FakeGitHub:
    def __init__(self,private=True):
        self.private=private;self.releases=[];self.blobs={};self.requests=[];self.status=None

    def seed(self,original,draft=False):
        blob=seal(original,KEY);ident=len(self.releases)+1;asset_id=100+ident
        release=dict(id=ident,tag_name=PREFIX+original['coverage']['day'],draft=draft,
            body=canonical(dict(schema='owned-release-v2',day=original['coverage']['day'],sha256=hashlib.sha256(blob).hexdigest(),bytes=len(blob))).decode(),
            assets=[dict(id=asset_id,name=ASSET,size=len(blob))])
        self.releases.append(release);self.blobs[asset_id]=blob
        return release

    def __call__(self,request):
        self.requests.append(request);path=request.url.path;method=request.method
        assert request.url.host in {'api.github.com','uploads.github.com'}
        if self.status:return httpx.Response(self.status)
        if path=='/repos/test/private':return httpx.Response(200,json={'private':self.private,'full_name':'test/private'})
        if path.endswith('/releases') and method=='GET':return httpx.Response(200,json=self.releases)
        if '/releases/assets/' in path:return httpx.Response(200,content=self.blobs[int(path.split('/')[-1])])
        if path.endswith('/releases') and method=='POST':
            params=json.loads(request.content);params.update(id=len(self.releases)+1,assets=[])
            self.releases.append(params);return httpx.Response(201,json=params)
        ident=int(path.split('/')[-2] if path.endswith('/assets') else path.split('/')[-1])
        release=next(r for r in self.releases if r['id']==ident)
        if path.endswith('/assets'):
            assert request.url.host=='uploads.github.com' and request.url.params['name']==ASSET
            unseal(request.content,KEY);asset_id=100+ident
            asset=dict(id=asset_id,name=ASSET,size=len(request.content));release['assets'].append(asset)
            self.blobs[asset_id]=request.content
            return httpx.Response(201,json=asset)
        if method=='PATCH':release.update(json.loads(request.content));return httpx.Response(200,json=release)
        if method=='DELETE':self.releases.remove(release);return httpx.Response(204)
        raise AssertionError((method,path))


def stub_source(monkeypatch,original):
    import src.archive.cli as cli
    @asynccontextmanager
    async def snapshot(url):yield object()
    async def collect(*args,**kwargs):return deepcopy(original)
    async def coverage(*args,**kwargs):return deepcopy(original['coverage'])
    monkeypatch.setattr(cli,'snapshot',snapshot);monkeypatch.setattr(cli,'collect',collect);monkeypatch.setattr(cli,'collect_coverage',coverage)


async def test_daily_publish_readback_restore_and_repeated_day(tmp_path,monkeypatch):
    fake=FakeGitHub();stub_source(monkeypatch,data())
    config=env(tmp_path)
    result=await daily(config,NOW,transport=httpx.MockTransport(fake))
    assert result==data() and len(fake.releases)==1 and not fake.releases[0]['draft']
    assert json.loads(Path(config['ARCHIVE_REPORT']).read_bytes())==data()['coverage']
    # Retry uses authenticated stored bytes, with no new DB read/upload.
    import src.archive.cli as cli
    def forbidden(*args,**kwargs):raise AssertionError('Source re-read on repeated day')
    monkeypatch.setattr(cli,'snapshot',forbidden)
    before=len([r for r in fake.requests if r.method=='POST'])
    config['ARCHIVE_REPORT']=str(tmp_path/'retry.json')
    assert await daily(config,NOW,transport=httpx.MockTransport(fake))==result
    assert len(fake.releases)==1 and len([r for r in fake.requests if r.method=='POST'])==before


@pytest.mark.parametrize('draft_assets',[False,True])
async def test_crash_draft_recovery(tmp_path,monkeypatch,draft_assets):
    fake=FakeGitHub();release=fake.seed(data(),draft=True)
    if not draft_assets:release['assets']=[]
    stub_source(monkeypatch,data())
    await daily(env(tmp_path),NOW,transport=httpx.MockTransport(fake))
    assert len(fake.releases)==1 and not release['draft'] and len(release['assets'])==1


@pytest.mark.parametrize('failure',['public','forbidden','quota','corrupt','outage','conflict'])
async def test_fail_closed_without_report(tmp_path,monkeypatch,failure):
    fake=FakeGitHub();stub_source(monkeypatch,data());config=env(tmp_path)
    if failure=='public':fake.private=False
    if failure=='forbidden':fake.status=403
    if failure=='quota':config['ARCHIVE_BUDGET_BYTES']='1'
    if failure=='corrupt':
        fake.seed(data());fake.blobs[101]=b'x'*len(fake.blobs[101])
    if failure=='outage':
        import src.archive.cli as cli
        @asynccontextmanager
        async def broken(url):raise OSError('synthetic DSN SECRET');yield
        monkeypatch.setattr(cli,'snapshot',broken)
    if failure=='conflict':fake.status=422
    with pytest.raises((ValueError,OSError)):await daily(config,NOW,transport=httpx.MockTransport(fake))
    assert not Path(config['ARCHIVE_REPORT']).exists()
    assert not any(r.method=='DELETE' for r in fake.requests)


async def test_retention_only_authenticated_aged_own_releases(tmp_path,monkeypatch):
    fake=FakeGitHub();old=fake.seed(data(day='2026-09-01'));recent=fake.seed(data(day='2026-10-23'))
    legacy=dict(id=900,tag_name='legacy-archive',assets=[],draft=False);fake.releases.append(legacy)
    stub_source(monkeypatch,data())
    await daily(env(tmp_path),NOW,transport=httpx.MockTransport(fake))
    assert old not in fake.releases and recent in fake.releases and legacy in fake.releases


async def test_upload_primitive_rejects_plaintext_before_any_network():
    fake=FakeGitHub()
    archive=ReleaseArchive('test/private','synthetic',KEY,budget_bytes=1000000,keep_days=30,transport=httpx.MockTransport(fake))
    try:
        with pytest.raises(ValueError):await archive.publish(data(),b'Goaloo raw prices',[],0)
        assert not fake.requests
    finally:await archive.close()


async def test_corrupt_server_readback_leaves_draft_and_no_report(tmp_path,monkeypatch):
    fake=FakeGitHub();stub_source(monkeypatch,data())
    def corrupt(request):
        response=fake(request)
        if '/releases/assets/' in request.url.path:return httpx.Response(200,content=b'x'*len(response.content))
        return response
    config=env(tmp_path)
    with pytest.raises(ValueError):await daily(config,NOW,transport=httpx.MockTransport(corrupt))
    assert fake.releases[0]['draft'] and not Path(config['ARCHIVE_REPORT']).exists()


async def test_coverage_counts_include_empty_league_and_no_match_identity(db):
    from tests.test_performance_status import fixture
    from src.models import League
    from src.archive.coverage import collect_coverage
    await fixture(db)
    async with db.session() as session:
        session.add(League(external_id=9999,name='PRIVATE_NAME',country='synthetic',enabled=True))
        await session.commit()
    async with db.engine.connect() as conn:
        class Connection:
            execute=conn.execute
            async def scalar(self,statement):return 110
        report=await collect_coverage(Connection(),NOW.date(),1000,dict(day='2026-10-21',database_bytes=100),expected_leagues=[9999,9998])
    assert report['growth_bytes']==10 and report['missing_run_days']==2
    assert report['missing_league_codes']==[9998]
    empty=next(r for r in report['leagues'] if r['league_code']==9999)
    assert empty['fixtures']==0 and empty['newest_fixture_date'] is None
    assert not any(x in canonical(report).decode() for x in ('PRIVATE_NAME','match_id','raw','odds_price','home_team'))
    original=data();original['coverage']=report;validate(original)


@pytest.mark.parametrize('kind',['fresh','missed','forbidden','empty'])
async def test_independent_missing_run_watchdog(kind):
    def respond(request):
        assert request.method=='GET'
        if kind=='forbidden':return httpx.Response(403)
        rows=[] if kind=='empty' else [dict(event='schedule',conclusion='success',updated_at=(NOW-timedelta(hours=48 if kind=='missed' else 1)).isoformat())]
        return httpx.Response(200,json={'workflow_runs':rows})
    if kind=='fresh':await check('test/private','synthetic',now=NOW,transport=httpx.MockTransport(respond))
    else:
        with pytest.raises(ValueError):await check('test/private','synthetic',now=NOW,transport=httpx.MockTransport(respond))


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='Isolated PostgreSQL CI required')
async def test_real_postgres_readonly_role_checks_and_column_write_rejection():
    from src.db import Database
    db=Database(os.environ['TEST_POSTGRES_URL']);role='audit_test_'+uuid.uuid4().hex
    try:
        await db.initialize()
        async with db.engine.begin() as conn:
            await conn.execute(text(f'CREATE ROLE {role} NOLOGIN'))
            await conn.execute(text(f'GRANT SELECT (id) ON leagues TO {role}'))
        async with db.engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text('SET TRANSACTION READ ONLY'))
                await conn.execute(text(f'SET LOCAL ROLE {role}'))
                await validate_reader(conn)
                assert await conn.scalar(text('SHOW transaction_read_only'))=='on'
                with pytest.raises(Exception):await conn.execute(text('DELETE FROM leagues'))
        async with db.engine.begin() as conn:await conn.execute(text(f'GRANT UPDATE (name) ON leagues TO {role}'))
        async with db.engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(f'SET LOCAL ROLE {role}'))
                with pytest.raises(ValueError):await validate_reader(conn)
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY {role}'))
            await conn.execute(text(f'DROP ROLE {role}'))
        await db.close()
