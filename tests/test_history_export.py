"""PR2 synthetic snapshots and disposable PostgreSQL. No production side effects."""
import base64
from copy import deepcopy
from datetime import timedelta
import os
import uuid
import pytest
from sqlalchemy import select,text,DateTime
from sqlalchemy.ext.asyncio import create_async_engine
from src.models import Base,Match,MatchStatistics
from tests.test_history_readthrough import fixture_records,KEY,NOW
from src.archive.history_format import canonical,parse,seal,unseal,sha
from src.archive.history_plan import MARKETS,HistorySource,plan,bundle
from src.archive.history_export import export,publish_partition
from src.archive.history_index import HistoryIndex
from src.archive.history_postgres import verify_postgres,verification_engine


class Source:
    namespace='isolated_source'
    def __init__(self,records):self.records=records;self.finger='one'
    async def fingerprint(self):return self.finger
    async def inventory(self):
        from src.archive.history_format import timestamp
        return [{**r['row'],'kickoff_at':timestamp(r['row']['kickoff_at'])} for r in self.records if r['table']=='matches']
    async def rows(self,name,column=None,keys=()):
        return [(r['row'],canonical(r)) for r in self.records if r['table']==name and (column is None or r['row'][column] in keys)]
    async def reports_for(self,ids):
        return [r['row']['id'] for r in self.records if r['table']=='learning_reports' and any(x['match_id'] in ids for x in r['row']['source_manifest'])]
    async def processing_complete(self,ids,reports):return True


class Store:
    key=KEY
    def __init__(self):self.releases={};self.blobs={};self.counter=0;self.fail=None;self.finished=[]
    async def open(self,generation,create=False):
        self.archive_id=generation
        self.release=self.releases.setdefault(generation,{'draft':True,'assets':{}})
        self.assets=self.release['assets']
    async def get(self,name):
        if name in self.assets:return self.blobs[self.assets[name]['id']]
    async def put_raw(self,name,raw):
        existing=await self.get(name)
        if existing:
            if unseal(existing,KEY,self.archive_id,name)!=raw:raise ValueError('Immutable conflict')
            return existing,self.assets[name]
        if self.fail==name:raise ValueError('Simulated interruption')
        assert self.release['draft']
        self.counter+=1;blob=seal(raw,KEY,self.archive_id,name)
        meta={'id':self.counter,'size':len(blob),'name':name}
        self.assets[name]=meta;self.blobs[self.counter]=blob
        return blob,meta
    async def finish(self,proof=None):
        if proof is not None:self.release['body']='history-proof-v1:'+base64.b64encode(seal(canonical(proof),KEY,self.archive_id,'summary.enc')).decode()
        self.release['draft']=False;self.finished.append(self.archive_id)


async def records(db):
    rows,mid,lid=await fixture_records(db)
    match=next(r['row'] for r in rows if r['table']=='matches')
    match.update(ht_home=0,ht_away=0)
    match['raw']['archive_finality']={'observed_at':(NOW-timedelta(days=1)).isoformat(),'events_final':True,
        'markets':{k:{'status':'unavailable','source_reference':'synthetic-provider-absence'} for k in MARKETS}}
    # Complete nullable schema with actual fixture-only zero observations.
    table=Base.metadata.tables['match_statistics']
    stats={c.name:None for c in table.c}
    stats.update(match_id=mid,is_final=True,collection_status='complete',source='fixture',raw={},updated_at=(NOW-timedelta(days=1)).isoformat())
    for k in ('home_corners','away_corners','home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards'):stats[k]=0
    rows.append({'table':'match_statistics','row':stats})
    return rows,mid,lid


async def verifier(catalog,chunks):
    from src.archive.history_restore import restore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as root:restore(Path(root)/'test.sqlite',catalog,chunks)
    return dict(postgres_restore=True,read_through=True,exact_bytes=True,counts=catalog['counts'],generation=catalog['generation'])


async def test_partition_resume_atomic_root_and_coverage(db):
    rows,mid,lid=await records(db);source=Source(rows);store=Store()
    store.fail='catalog.enc'
    with pytest.raises(ValueError):await export(source,store,'daily-fixture',NOW,verifier)
    assert 'daily-fixture' not in store.finished
    before=len(store.blobs);store.fail=None
    report=await export(source,store,'daily-fixture',NOW+timedelta(hours=1),verifier)
    assert report['status']=='verified' and report['covered']==1 and report['source_space_reclaimed']==0
    assert store.finished[-1]=='daily-fixture'
    blobs=len(store.blobs)
    assert (await export(source,store,'daily-fixture',NOW+timedelta(hours=2),verifier))==report
    assert len(store.blobs)==blobs and blobs>before
    class Objects:
        async def get(self,meta):return store.blobs[meta['id']]
        async def close(self):pass
    index=HistoryIndex(Objects(),KEY,'daily-fixture',{'id':report['index_asset_id'],'size':report['index_bytes']},report['index_sha256'])
    data=await index.prepare()
    generation=data['partitions'][0]['generation']
    assert data['partitions'][0]['matches'][0]['identity']==f'isolated_source:999003'
    async with index.session(generation) as (session,reader):
        assert (await session.get(Match,mid)).external_match_id==999003
    await index.close()
    source.finger='another-database'
    with pytest.raises(ValueError,match='database changed'):await export(source,store,'daily-fixture',NOW,verifier)


async def test_unverified_and_unsettled_records_retained_without_synthetic_values(db):
    rows,mid,lid=await records(db)
    match=next(r['row'] for r in rows if r['table']=='matches')
    del match['raw']['archive_finality']['markets'][MARKETS[0]]
    result=[item async for item in plan(Source(rows),NOW)]
    assert len(result)==1 and result[0]['covered_ids']==[]
    assert 'unverified_market' in result[0]['retained'][0]['reason']
    assert match['ht_home']==0  # no exporter mutation


async def test_no_publication_if_restore_smoke_fails(db):
    rows,_,_=await records(db);store=Store()
    async def fail(*args):return {'postgres_restore':False}
    with pytest.raises(ValueError,match='PostgreSQL verification'):await export(Source(rows),store,'fail-fixture',NOW,fail)
    assert not store.finished
    assert all('catalog.enc' not in r['assets'] and 'index.enc' not in r['assets'] for r in store.releases.values())


def test_restore_rejects_production_host():
    for url in ('postgresql://x:y@production.example/history_verify','postgresql://x:y@localhost/production'):
        with pytest.raises(ValueError):verification_engine(url)


async def test_real_postgres_export_restore_and_read_through(db,monkeypatch):
    url=os.getenv('TEST_POSTGRES_URL')
    if not url:pytest.skip('TEST_POSTGRES_URL not configured')
    monkeypatch.setenv('HISTORY_RESOLVER_ENABLED','true')
    from src.archive.history_format import timestamp
    rows,mid,lid=await records(db)
    engine=create_async_engine(url)
    schema='history_source_'+uuid.uuid4().hex
    try:
        async with engine.connect() as conn:
            tx=await conn.begin()
            try:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                scoped=await conn.execution_options(schema_translate_map={None:schema})
                await scoped.run_sync(Base.metadata.create_all)
                for table in Base.metadata.sorted_tables:
                    for record in rows:
                        if record['table']!=table.name:continue
                        values=dict(record['row'])
                        for col in table.c:
                            if isinstance(col.type,DateTime) and values[col.name] is not None:values[col.name]=timestamp(values[col.name])
                        await scoped.execute(table.insert().values(**values))
                raw=canonical(next(r['row']['raw'] for r in rows if r['table']=='matches')).decode()
                raw=raw[:-1]+',"precision_fixture":0.12345678901234567890123456789}'
                await conn.execute(text(f'UPDATE "{schema}".matches SET raw=CAST(:raw AS json)'),{'raw':raw})
                # Commit the isolated test schema so source can use READ ONLY.
                await tx.commit()
            except BaseException:
                await tx.rollback();raise
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
                source=HistorySource(conn,'pg-fixture',schema)
                assert b'0.12345678901234567890123456789' in (await source.rows('matches','id',[mid]))[0][1]
                store=Store()
                async def real_verify(catalog,chunks):return await verify_postgres(url,catalog,chunks)
                report=await export(source,store,'pg-fixture-run',NOW,real_verify)
                assert report['covered']==1 and report['status']=='verified'
                assert await conn.scalar(text(f'SELECT count(*) FROM "{schema}".matches'))==1
        async with engine.connect() as conn:
            assert not await conn.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'hist_restore_%'"))
    finally:
        async with engine.begin() as conn:await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_github_staging_encrypted_readback_budget_and_no_deletion(db):
    import httpx
    import json
    from src.archive.history_export import HistoryStore
    releases=[];blobs={};requests=[]
    def transport(request):
        path=request.url.path;method=request.method;requests.append((method,path))
        assert method!='DELETE'
        if path=='/repos/test/private':return httpx.Response(200,json={'private':True,'full_name':'test/private','permissions':{'push':True}})
        if '/releases/assets/' in path:return httpx.Response(200,content=blobs[int(path.rsplit('/',1)[1])])
        if path.endswith('/releases'):
            if method=='GET':return httpx.Response(200,json=releases)
            row={**json.loads(request.content),'id':len(releases)+1,'assets':[]};releases.append(row)
            return httpx.Response(201,json=row)
        ident=int(path.split('/')[-2] if path.endswith('/assets') else path.split('/')[-1])
        release=next(r for r in releases if r['id']==ident)
        if path.endswith('/assets'):
            if method=='GET':return httpx.Response(200,json=release['assets'])
            assert request.content.startswith(b'KMH1') and request.url.host=='uploads.github.com'
            key=len(blobs)+1;blobs[key]=request.content
            asset={'id':key,'name':request.url.params['name'],'size':len(request.content)}
            release['assets'].append(asset);return httpx.Response(201,json=asset)
        if method=='PATCH':release.update(json.loads(request.content));return httpx.Response(200,json=release)
        raise AssertionError((method,path))
    rows,_,_=await records(db)
    client=HistoryStore('test/private','fixture-only',KEY,budget_bytes=10_000_000,keep_days=3650,transport=httpx.MockTransport(transport))
    try:
        result=await export(Source(rows),client,'http-fixture',NOW,verifier)
        assert result['status']=='verified'
        posts=len([r for r in requests if r[0]=='POST'])
        assert await export(Source(rows),client,'http-fixture',NOW,verifier)==result
        assert len([r for r in requests if r[0]=='POST'])==posts
        client.budget=1
        with pytest.raises(ValueError,match='budget'):await client.open('new-fixture',create=True)
    finally:await client.close()


async def test_no_eligible_run_stays_draft(db):
    rows,_,_=await records(db)
    next(r['row'] for r in rows if r['table']=='matches')['status']='live'
    store=Store();result=await export(Source(rows),store,'no-eligible',NOW,verifier)
    assert result['status']=='no_eligible_records' and result['covered']==0
    assert store.release['draft'] and not store.finished
