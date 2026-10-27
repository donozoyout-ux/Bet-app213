"""Only isolated data and MockTransport; never contacts providers or production."""
import asyncio
import base64
import json
import os
import sqlite3
import uuid
import httpx
import pytest
from cryptography.exceptions import InvalidTag
from src.archive.board_format import canonical, seal, unseal, frame, records
from src.archive.board_backup import BoardStore, BoardSource, export, verify

KEY = base64.urlsafe_b64encode(b'a'*32).decode()


class Source:
    def __init__(self, count=5):
        self.rows = {f'{i:064x}':canonical(dict(version=f'{i:064x}',league_id=1,
            created_at='2026-10-27T00:00:00+00:00',payload={'test_only':[i,'Türkçe',None]})) for i in range(count)}
    async def identity(self): return 'isolated-source'
    async def keys(self): return sorted(self.rows)
    async def read(self, keys): return b''.join(frame(self.rows[k]) for k in keys if k in self.rows)


class GitHub:
    def __init__(self):
        self.releases=[];self.blobs={};self.private=True;self.requests=[];self.corrupt=False
    def __call__(self, request):
        method,path=request.method,request.url.path
        self.requests.append((method,path))
        assert method!='DELETE'
        if path=='/repos/test/private':
            return httpx.Response(200,json=dict(private=self.private,archived=False,full_name='test/private',permissions={'push':True}))
        if '/releases/assets/' in path:
            blob=self.blobs[int(path.rsplit('/',1)[-1])]
            return httpx.Response(200,content=b'x'*len(blob) if self.corrupt else blob)
        if path.endswith('/releases'):
            if method=='GET':return httpx.Response(200,json=self.releases)
            release={**json.loads(request.content),'id':len(self.releases)+1,'assets':[]}
            self.releases.append(release);return httpx.Response(201,json=release)
        ident=int(path.split('/')[-2] if path.endswith('/assets') else path.split('/')[-1])
        release=next(r for r in self.releases if r['id']==ident)
        if path.endswith('/assets'):
            if method=='GET':
                page=int(request.url.params.get('page','1'));return httpx.Response(200,json=release['assets'][(page-1)*100:page*100])
            assert request.url.host=='uploads.github.com' and request.content.startswith(b'KAB1')
            aid=len(self.blobs)+1
            asset=dict(id=aid,name=request.url.params['name'],size=len(request.content))
            release['assets'].append(asset);self.blobs[aid]=request.content
            return httpx.Response(201,json=asset)
        if method=='PATCH':
            release.update(json.loads(request.content));return httpx.Response(200,json=release)
        raise AssertionError((method,path))


def store(fake, budget=10000000):
    return BoardStore('test/private','test-token',KEY,budget_bytes=budget,keep_days=3650,transport=httpx.MockTransport(fake))


async def test_resume_frozen_keys_exact_restore_and_idempotency(tmp_path):
    fake=GitHub();source=Source();client=store(fake)
    try:
        await client.open('test',create=True)
        result=await export(source,client,KEY,'test',rows_per_chunk=2,max_new_chunks=1)
        assert result['status']=='incomplete' and fake.releases[0]['draft']
        assert (await verify(client,KEY,'test'))['missing']
    finally:await client.close()
    # A new process resumes using server assets; later inserted keys are outside
    # the frozen inventory, even when they sort inside/before its upper bound.
    source.rows['f'*64]=canonical(dict(version='f'*64,league_id=1,created_at='later',payload={}))
    client=store(fake)
    try:
        await client.open('test',create=True)
        result=await export(source,client,KEY,'test')
        assert result['rows']==5 and result['restore_verified'] and not result['full_database_backup']
        assert not fake.releases[0]['draft']
        before=len([r for r in fake.requests if r[0]=='POST'])
        assert await export(source,client,KEY,'test')==result
        assert len([r for r in fake.requests if r[0]=='POST'])==before
        target=tmp_path/'restored.sqlite'
        assert await verify(client,KEY,'test',target)==result
        with sqlite3.connect(target) as c:
            actual=dict(c.execute('SELECT version,row_json FROM board_editions_archive'))
            assert actual=={k:v for k,v in source.rows.items() if k!='f'*64}
            with pytest.raises(sqlite3.DatabaseError):c.execute('DELETE FROM board_editions_archive')
        with pytest.raises(FileExistsError):await verify(client,KEY,'test',target)
    finally:await client.close()


@pytest.mark.parametrize('failure',['source_change','missing_row','corrupt','wrong_key','wrong_id','missing_chunk','manifest'])
async def test_fail_closed_integrity(failure):
    fake=GitHub();source=Source();client=store(fake)
    try:
        await client.open('test',create=True)
        await export(source,client,KEY,'test')
        if failure=='source_change':source.rows['0'*64]=source.rows['0'*64].replace(b'T\xc3\xbcrk\xc3\xa7e',b'changed')
        if failure=='missing_row':del source.rows['0'*64]
        if failure in {'source_change','missing_row'}:
            with pytest.raises(ValueError):await export(source,client,KEY,'test')
            return
        if failure=='missing_chunk':
            del client.assets['chunk-000000.enc']
            assert (await verify(client,KEY,'test'))['status']=='incomplete';return
        if failure=='corrupt':fake.corrupt=True
        if failure=='manifest':
            aid=client.assets['manifest.enc']['id'];blob=fake.blobs[aid]
            data=json.loads(unseal(blob,KEY,'test','manifest.enc'));data['row_count']+=1
            fake.blobs[aid]=seal(canonical(data),KEY,'test','manifest.enc');client.assets['manifest.enc']['size']=len(fake.blobs[aid])
        key=base64.urlsafe_b64encode(b'b'*32).decode() if failure=='wrong_key' else KEY
        with pytest.raises((ValueError,InvalidTag)):
            await verify(client,key,'other' if failure=='wrong_id' else 'test')
    finally:await client.close()


@pytest.mark.parametrize('failure',['public','budget','readback'])
async def test_private_budget_and_readback_guards(failure):
    fake=GitHub();fake.private=failure!='public'
    client=store(fake,budget=1 if failure=='budget' else 1000000)
    try:
        with pytest.raises(ValueError):
            await client.open('test',create=True)
            if failure=='readback':fake.corrupt=True
            await export(Source(),client,KEY,'test')
        assert not fake.releases or fake.releases[0]['draft']
    finally:await client.close()


async def test_more_than_one_page_of_assets_is_verified():
    fake=GitHub();client=store(fake)
    try:
        await client.open('pages',create=True)
        await export(Source(105),client,KEY,'pages',rows_per_chunk=1,max_new_chunks=128)
    finally:await client.close()
    client=store(fake)
    try:
        await client.open('pages')
        assert len(client.assets)==107 and (await verify(client,KEY,'pages'))['rows']==105
    finally:await client.close()


async def test_upload_rejects_plaintext_before_mutation():
    fake=GitHub();client=store(fake)
    try:
        await client.open('test',create=True)
        calls=len(fake.requests)
        with pytest.raises(ValueError):await client.put('chunk-000000.enc',b'plaintext dump')
        assert len(fake.requests)==calls
    finally:await client.close()


def test_encryption_context_and_decompression_limit(monkeypatch):
    import src.archive.board_format as fmt
    blob=seal(b'x'*4096,KEY,'test','chunk-000000.enc')
    with pytest.raises(InvalidTag):unseal(blob,KEY,'test','chunk-000001.enc')
    monkeypatch.setattr(fmt,'MAX_BYTES',1024)
    with pytest.raises(ValueError):unseal(blob,KEY,'test','chunk-000000.enc')
    with pytest.raises(ValueError):seal(b'x'*1025,KEY,'test','chunk-000000.enc')


def test_lossless_multiline_json_and_frame_boundaries():
    row=json.dumps(json.loads(Source(1).rows['0'*64]),indent=2).encode()
    assert records(frame(row),['0'*64]) == [row]
    for broken in (frame(row)[:-1],frame(row)+b'extra',b'\xff\xff\xff\xff'):
        with pytest.raises(ValueError):records(broken,['0'*64])


def test_plan_rejects_unmanageable_asset_inventory():
    from src.archive.board_backup import validate_plan,TABLE_SCHEMA,SCHEMA
    with pytest.raises(ValueError,match='asset inventory'):
        validate_plan(dict(schema=SCHEMA,archive_id='test',table='board_editions',columns=TABLE_SCHEMA,
            keys=[f'{i:064x}' for i in range(9999)],rows_per_chunk=1),'test')


async def test_postgres_source_readonly_lossless():
    if not os.getenv('TEST_POSTGRES_URL'):pytest.skip('TEST_POSTGRES_URL not configured')
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    engine=create_async_engine(os.environ['TEST_POSTGRES_URL'])
    schema='backup_'+uuid.uuid4().hex
    fake=GitHub();client=store(fake)
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(text(f'CREATE TABLE "{schema}".board_editions (version varchar(64) PRIMARY KEY,league_id integer,created_at timestamptz,payload json)'))
            await c.execute(text(f'INSERT INTO "{schema}".board_editions VALUES (:version,1,CURRENT_TIMESTAMP,CAST(:payload AS json))'),
                {'version':'a'*64,'payload':'{"decimal":0.1234567890123456789012345,"empty":null}'})
        await client.open('pg',create=True)
        async with engine.connect() as c:
            async with c.begin():
                await c.execute(text('SET TRANSACTION READ ONLY'))
                assert (await export(BoardSource(c,schema),client,KEY,'pg'))['rows']==1
        raw=unseal(await client.get('chunk-000000.enc'),KEY,'pg','chunk-000000.enc')
        assert b'0.1234567890123456789012345' in raw
    finally:
        await client.close()
        async with engine.begin() as c:await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_disk_full_latches_startup_and_readiness(monkeypatch):
    from types import SimpleNamespace
    from httpx import AsyncClient,ASGITransport
    import src.api.main as main
    import src.api.routes as routes
    class Full(Exception):sqlstate='53100'
    calls=[]
    async def initialize():calls.append(1);raise Full('not logged')
    db=SimpleNamespace(ready=False,configured=True,initialize=initialize)
    monkeypatch.setattr(main,'database',db);monkeypatch.setattr(routes,'database',db)
    task=asyncio.create_task(main.supervise_database())
    try:
        for _ in range(10):await asyncio.sleep(0)
        assert calls==[1] and db.storage_blocked and not db.ready
        async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as api:
            assert (await api.get('/health')).status_code==200
            assert (await api.get('/ready')).json()['reason']=='database_storage_full'
            assert (await api.post('/api/coupons/automatic')).json()['code']=='database_storage_full'
        assert calls==[1]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


async def test_readiness_checks_actual_schema(api,db,monkeypatch):
    import src.api.main as main
    from sqlalchemy import text
    monkeypatch.setattr(main,'database',db)
    assert (await api.get('/ready')).status_code==200
    async with db.engine.begin() as c:
        await c.execute(text('ALTER TABLE prediction_publications RENAME TO missing_publications'))
    assert (await api.get('/ready')).json()['reason']=='publication_schema_missing'


def test_wrapped_disk_full_is_detected_without_message_matching():
    from src.api.main import storage_exhausted
    from types import SimpleNamespace
    assert storage_exhausted(SimpleNamespace(orig=SimpleNamespace(pgcode='53100')))
    assert not storage_exhausted(ValueError('No space left on device'))
