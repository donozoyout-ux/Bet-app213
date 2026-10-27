from datetime import timedelta,datetime,timezone
import os
import pytest
from sqlalchemy import select,insert,text
from src.models import Match,Team,League,Season,BoardEdition
from src.archive.history_index import HistoryIndex
from src.archive.history_format import canonical,seal,sha
from src.archive.history_reader import HistoryUnavailable
from src.archive.history_resolver import engine_rows,site_history
from src.jobs.storage_admission import StorageAdmissionError
from tests.test_history_export import records,Source,Store,export,verifier,KEY,NOW


async def index_for(db):
    rows,mid,lid=await records(db);store=Store()
    # Freeze observation time before the test as-of boundary.
    for record in rows:
        for k in ('created_at','updated_at','last_scraped_at'):
            if record['row'].get(k):record['row'][k]=(NOW-timedelta(days=1)).isoformat()
    report=await export(Source(rows),store,'resolver-fixture',NOW,verifier)
    class Objects:
        calls=0
        async def get(self,meta):self.calls+=1;return store.blobs[meta['id']]
        async def close(self):pass
    index=HistoryIndex(Objects(),KEY,'resolver-fixture',{'id':report['index_asset_id'],'size':report['index_bytes']},report['index_sha256'])
    return index,mid,lid


async def test_resolver_preserves_hot_precedence_and_namespace_collision(db,monkeypatch):
    import src.archive.history_resolver as resolver
    index,mid,lid=await index_for(db)
    monkeypatch.setattr(resolver,'configured_index',lambda:index)
    try:
        async with db.session() as session:
            hot=await session.get(Match,mid)
            # Same local ID now identifies a different fixture in the new DB.
            hot.external_match_id=111111;hot.status='scheduled';hot.kickoff_at=NOW+timedelta(days=1)
            await session.commit()
            rows=await engine_rows(session,lid,NOW,-999)
            assert len(rows)==1 and rows[0][0].id==-999003
            assert rows[0][0].home_team_id==hot.home_team_id
            page=await site_history(session,hot,'h2h',10,0,NOW)
            assert len(page['items'])==1 and page['items'][0]['archive_generation']
            calls=index.objects.calls
            again=await site_history(session,hot,'h2h',10,0,NOW)
            assert again==page and index.objects.calls==calls
            hot.external_match_id=999003  # Available hot observation wins even if live.
            await session.commit()
        async with db.session() as session:
            assert await engine_rows(session,lid,NOW,-999)==[]
    finally:await index.close()


async def test_query_scope_budget_never_returns_partial_history(db):
    index,_,_=await index_for(db)
    try:
        data=await index.prepare()
        data['partitions']=[dict(data['partitions'][0],generation=str(i)) for i in range(9)]
        with pytest.raises(HistoryUnavailable,match='eight scopes'):await index.selected()
    finally:await index.close()


async def test_hot_insert_guard_existing_records_preserved(db,monkeypatch):
    from tests.test_history_readthrough import fixture_records
    _,mid,lid=await fixture_records(db)
    monkeypatch.setenv('HISTORY_HOT_TIER','true')
    async with db.session() as session:
        match=await session.get(Match,mid)
        match.ft_home=2
        await session.commit()  # Existing unresolved data may be completed.
        clone={c.name:getattr(match,c.name) for c in Match.__table__.c if c.name!='id'}
        clone['external_match_id']=234567
        with pytest.raises(StorageAdmissionError,match='historical_insert'):
            await session.execute(insert(Match).values(**clone))
        await session.rollback()
        assert (await session.get(Match,mid)).ft_home==2
        assert await session.scalar(select(Match.id).where(Match.external_match_id==234567)) is None
    from src.jobs.worker import enqueue
    with pytest.raises(StorageAdmissionError,match='historical_import_disabled'):await enqueue('backfill')


async def test_index_http_coverage_and_partition_namespace(db,monkeypatch):
    import httpx
    from src.api.main import app
    import src.api.history_catalog as api
    import src.archive.history_index as module
    from src.api import routes
    index,mid,lid=await index_for(db)
    monkeypatch.setattr(api,'database',db);monkeypatch.setattr(routes,'database',db)
    monkeypatch.setattr(api,'configured_index',lambda:index);monkeypatch.setattr(module,'configured_index',lambda:index)
    monkeypatch.setenv('HISTORY_INDEX_ENABLED','true')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            coverage=(await client.get('/api/history/coverage')).json()
            assert coverage['covered']==1 and coverage['verified_production_archive'] is True
            gen=coverage['partitions'][0]['generation']
            data=(await client.get(f'/api/matches/{mid}/dossier?history_source=archive&history_generation={gen}')).json()
            assert data['match']['external_match_id']==999003
            assert (await client.get('/api/history/resolve/999003')).json()['source']=='hot'
            db.ready=False
            resolved=(await client.get('/api/history/resolve/999003')).json()
            assert resolved['source']=='archive' and 'history_generation=' in resolved['href']
            assert 'asset_id' not in str(coverage) and KEY not in str(coverage)
    finally:await index.close()


async def test_real_postgres_capacity_guard_rejects_without_deletion(monkeypatch):
    url=os.getenv('TEST_POSTGRES_URL')
    if not url:pytest.skip('TEST_POSTGRES_URL not configured')
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.jobs.storage_admission import install,status
    import uuid
    engine=create_async_engine(url)
    install(engine.sync_engine)
    schema='capacity_'+uuid.uuid4().hex
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'CREATE TABLE "{schema}".evidence (id integer primary key)'))
            await conn.execute(text(f'INSERT INTO "{schema}".evidence VALUES (1)'))
        monkeypatch.setenv('HISTORY_HOT_TIER','true');monkeypatch.setenv('HISTORY_DB_STOP_BYTES','1')
        async with engine.begin() as conn:
            with pytest.raises(StorageAdmissionError,match='high_watermark'):
                await conn.execute(text(f'INSERT INTO "{schema}".evidence VALUES (2)'))
            assert await conn.scalar(text(f'SELECT count(*) FROM "{schema}".evidence'))==1
        assert status(engine)['blocked'] is True
    finally:
        monkeypatch.setenv('HISTORY_HOT_TIER','false')
        async with engine.begin() as conn:await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_worker_capacity_gate_does_not_require_a_third_connection(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import src.jobs.worker as worker
    import src.jobs.storage_admission as admission
    active=0;checked=[]
    class Session:
        async def get(self,*args):return SimpleNamespace(kind='backfill')
    class DB:
        @asynccontextmanager
        async def session(self):
            nonlocal active
            active+=1
            try:yield Session()
            finally:active-=1
    db=DB()
    async def gate(database):
        assert database is db and active==0
        checked.append(True)
    monkeypatch.setattr(worker,'database',db)
    monkeypatch.setattr(admission,'collector_gate',gate)
    monkeypatch.setenv('HISTORY_HOT_TIER','true')
    await worker.run_job(123)
    assert checked==[True] and active==0


async def test_real_postgres_board_cooldown_preserves_first_edition(monkeypatch):
    url=os.getenv('TEST_POSTGRES_URL')
    if not url:pytest.skip('TEST_POSTGRES_URL not configured')
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.models import Base
    from src.jobs.storage_admission import install
    import uuid
    engine=create_async_engine(url);install(engine.sync_engine)
    schema='board_guard_'+uuid.uuid4().hex
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            scoped=await conn.execution_options(schema_translate_map={None:schema})
            await scoped.run_sync(Base.metadata.create_all)
            await scoped.execute(insert(League).values(id=1,external_id=99999,name='fixture',country='fixture'))
        monkeypatch.setenv('HISTORY_HOT_TIER','true')
        async with engine.begin() as conn:
            await conn.execute(text(f'SET LOCAL search_path="{schema}",pg_catalog'))
            scoped=await conn.execution_options(schema_translate_map={None:schema})
            await scoped.execute(insert(BoardEdition).values(version='a'*64,league_id=1,created_at=datetime.now(timezone.utc),payload={'fixture':True}))
            with pytest.raises(StorageAdmissionError,match='cooldown'):
                await scoped.execute(insert(BoardEdition).values(version='b'*64,league_id=1,created_at=datetime.now(timezone.utc),payload={'fixture':2}))
            assert list(await scoped.scalars(select(BoardEdition.version)))==['a'*64]
    finally:
        monkeypatch.setenv('HISTORY_HOT_TIER','false')
        async with engine.begin() as conn:await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
