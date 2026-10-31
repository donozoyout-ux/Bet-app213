"""Storage incident regressions; synthetic fixtures and disposable PostgreSQL only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import uuid
import pytest
from sqlalchemy import insert, select, text, func
from sqlalchemy.ext.asyncio import create_async_engine
from src.jobs.storage_admission import enabled, board_limits, install, StorageAdmissionError
from src.models import Base, League, BoardEdition, BoardHead, PredictionBoard
from src.analytics.publications import publish_board
from tests.test_predictions import seed, NOW


def test_production_guard_cannot_be_disabled_by_legacy_flag(monkeypatch):
    monkeypatch.setenv('HISTORY_HOT_TIER','false')
    monkeypatch.setenv('APP_ENV','production')
    assert enabled()
    monkeypatch.setenv('APP_ENV','development');monkeypatch.setenv('RENDER','true')
    assert enabled()
    monkeypatch.setenv('HISTORY_BOARD_DOCUMENT_BYTES','99999999')
    monkeypatch.setenv('HISTORY_BOARD_DAILY_COUNT','99999')
    assert board_limits()['document_bytes']==131072 and board_limits()['daily_count']==64


async def test_clock_only_board_refresh_preserves_edition_and_payload(db):
    _,league,*_=await seed(db)
    async with db.session() as s:
        board=await s.get(PredictionBoard,league)
        data=deepcopy(board.payload);data['engine_analysis']=[{'cutoff':NOW.isoformat(),'sample_size':20}]
        version=await publish_board(s,board,NOW,candidate_payload=data);await s.commit()
        original=deepcopy((await s.scalar(select(BoardEdition.payload).where(BoardEdition.version==version))))
        before=await s.scalar(select(func.count()).select_from(BoardEdition))
        changed=deepcopy(data);changed['engine_analysis'][0]['cutoff']=(NOW+timedelta(minutes=1)).isoformat()
        assert await publish_board(s,board,NOW+timedelta(minutes=1),candidate_payload=changed)==version
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(BoardEdition))==before
        assert (await s.scalar(select(BoardEdition.payload).where(BoardEdition.version==version)))==original
        changed['engine_analysis'][0]['sample_size']=21
        assert await publish_board(s,board,NOW+timedelta(minutes=2),candidate_payload=changed)!=version


async def test_archive_preflight_never_claims_uploaded_or_restored(db):
    from tests.test_history_export import records,Source
    from src.archive.history_export import preflight
    from tests.test_history_export import NOW as ARCHIVE_NOW
    rows,_,_=await records(db)
    report=await preflight(Source(rows),ARCHIVE_NOW)
    assert report['status']=='planned_not_archived' and report['partitions']==1
    assert report['restore_verified'] is False and report['source_space_reclaimed']==0
    match=next(r['row'] for r in rows if r['table']=='matches')
    match['raw'].pop('archive_finality')
    report=await preflight(Source(rows),ARCHIVE_NOW)
    assert report['partitions']==0 and report['coverage']['retained']


async def test_real_postgres_storage_budgets_and_competing_writer(monkeypatch):
    url=os.getenv('TEST_POSTGRES_URL')
    if not url:pytest.skip('TEST_POSTGRES_URL not configured')
    engine=create_async_engine(url);install(engine.sync_engine)
    schema='containment_'+uuid.uuid4().hex
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            scoped=await c.execution_options(schema_translate_map={None:schema})
            await scoped.run_sync(Base.metadata.create_all)
            for lid in range(1,6):await scoped.execute(insert(League).values(id=lid,external_id=90000+lid,name='fixture',country='fixture'))
        monkeypatch.setenv('HISTORY_HOT_TIER','true')
        async with engine.begin() as c:
            await c.execute(text(f'SET LOCAL search_path="{schema}",pg_catalog'))
            scoped=await c.execution_options(schema_translate_map={None:schema})
            await scoped.execute(insert(BoardEdition).values(version='a'*64,league_id=1,created_at=datetime.now(timezone.utc),payload={'fixture':True}))
            async with engine.begin() as other:
                await other.execute(text(f'SET LOCAL search_path="{schema}",pg_catalog'))
                with pytest.raises(StorageAdmissionError,match='storage_writer_busy'):
                    await other.execute(text('UPDATE leagues SET name=name WHERE id=1'))
        async with engine.begin() as c:
            await c.execute(text(f'SET LOCAL search_path="{schema}",pg_catalog'))
            scoped=await c.execution_options(schema_translate_map={None:schema})
            with pytest.raises(StorageAdmissionError,match='document_budget'):
                await scoped.execute(insert(BoardEdition).values(version='b'*64,league_id=2,created_at=datetime.now(timezone.utc),payload={'large':'\u0131'*25000}))
            monkeypatch.setenv('HISTORY_BOARD_DAILY_COUNT','1')
            with pytest.raises(StorageAdmissionError,match='daily_budget'):
                await scoped.execute(insert(BoardEdition).values(version='c'*64,league_id=3,created_at=datetime.now(timezone.utc),payload={}))
            assert await scoped.scalar(select(func.count()).select_from(BoardEdition))==1
            with pytest.raises(StorageAdmissionError,match='raw_match_insert'):
                await c.execute(text('WITH injected AS (INSERT INTO matches DEFAULT VALUES RETURNING id) SELECT * FROM injected'))
    finally:
        monkeypatch.setenv('HISTORY_HOT_TIER','false')
        async with engine.begin() as c:await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_paused_worker_capacity_observation_is_read_only_and_cached(monkeypatch):
    from types import SimpleNamespace
    import src.api.history_catalog as api
    class Engine:
        dialect=SimpleNamespace(name='postgresql')
    database=SimpleNamespace(engine=Engine(),ready=True)
    calls=[]
    monkeypatch.setattr(api,'database',database)
    monkeypatch.setattr(api,'status',lambda engine:dict(enabled=True,blocked=bool(calls)))
    monkeypatch.setenv('HISTORY_INDEX_ENABLED','false')
    async def observe(db):
        assert db is database
        calls.append(True)
        raise StorageAdmissionError('database_high_watermark')
    monkeypatch.setattr(api,'collector_gate',observe)
    first=await api.coverage();second=await api.coverage()
    assert len(calls)==1 and first==second
    assert first['capacity']['blocked'] and not first['capacity']['observation_unavailable']
