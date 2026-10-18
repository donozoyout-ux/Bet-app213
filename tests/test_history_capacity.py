"""Capacity simulations never allocate/fill a real production database."""
import asyncio
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.schema import CreateSchema
from src.models import Base, League, Match, Season, Team, ScraperJob, JobItem, utcnow
import src.jobs.europe_capacity as capacity


class ProbeSession:
    bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
    no_autoflush = nullcontext()
    info = {}
    def __init__(self, value): self.value=value; self.calls=[]
    async def scalar(self, query, *args):
        self.calls.append(str(query))
        if 'pg_try_advisory_xact_lock' in str(query): return True
        return self.value


@pytest.mark.asyncio
@pytest.mark.parametrize('value',[None,0,-1,'600000000',True,Decimal('1.1'),Decimal('NaN'),700_000_000,711_485_004])
async def test_threshold_and_unavailable_metrics_fail_closed(value):
    session=ProbeSession(value)
    with pytest.raises(capacity.HistoryCapacityError): await capacity.check_capacity(session)
    assert 'pg_database_size(oid)' in session.calls[-1]
    assert 'current_database()' not in session.calls[-1]  # application may still be below 700 MB
    assert 'count(*)=count(bytes)' in session.calls[-1]  # no ignored NULL database size


@pytest.mark.asyncio
async def test_probe_is_fresh_after_commit_or_retry_and_stale_is_rejected(monkeypatch):
    session=ProbeSession(Decimal('699999999'))
    assert (await capacity.check_capacity(session))['database_bytes']==699999999
    session.value=700_000_000
    with pytest.raises(capacity.HistoryCapacityError): await capacity.check_capacity(session)
    assert len(session.calls)==4  # no cached positive admission
    session.value=600_000_000
    async def slow(_): await asyncio.sleep(.05); return 600_000_000
    monkeypatch.setattr(capacity,'aggregate_bytes',slow)
    monkeypatch.setattr(capacity,'PROBE_MAX_SECONDS',.01)
    with pytest.raises(capacity.HistoryCapacityError,match='unavailable'): await capacity.check_capacity(session)


@pytest.mark.asyncio
async def test_stale_return_and_probe_exception_are_sanitized(monkeypatch):
    session=ProbeSession(600_000_000)
    ticks=iter([0,3])
    # Replace the module's clock only, not asyncio's global clock.
    monkeypatch.setattr(capacity,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
    with pytest.raises(capacity.HistoryCapacityError,match='stale'): await capacity.check_capacity(session)
    monkeypatch.setattr(capacity,'time',SimpleNamespace(monotonic=lambda:0))
    async def failed(_): raise RuntimeError('postgres://secret-credential')
    monkeypatch.setattr(capacity,'aggregate_bytes',failed)
    with pytest.raises(capacity.HistoryCapacityError) as error: await capacity.check_capacity(session)
    assert 'secret' not in str(error.value)


@pytest_asyncio.fixture
async def pg_engine():
    value=os.getenv('TEST_POSTGRES_URL')
    if not value: pytest.skip('Requires isolated CI PostgreSQL')
    url=make_url(value)
    assert url.host in ('localhost','127.0.0.1') and url.database=='betapp_test'
    engine=create_async_engine(value)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine):
    # Even schema DDL is rolled back. No source/deletion commands are involved.
    async with pg_engine.connect() as connection:
        tx=await connection.begin()
        try:
            schema='capacity_'+uuid.uuid4().hex
            await connection.execute(CreateSchema(schema))
            mapped=await connection.execution_options(schema_translate_map={None:schema})
            await mapped.run_sync(Base.metadata.create_all)
            async with AsyncSession(mapped,expire_on_commit=False) as session:
                league=League(external_id=7,name='Synthetic Denmark',country='Denmark',enabled=True)
                session.add(league); await session.flush()
                yield session,league.id
        finally:
            await tx.rollback()


def fixture_data(age=60):
    return dict(external_match_id=123456,home_external_id=1001,away_external_id=1002,
        home_name='Synthetic home',away_name='Synthetic away',round=1,kickoff_at=utcnow()-timedelta(days=age),
        status='finished',ht_home=0,ht_away=0,ft_home=1,ft_away=0,raw={})


@pytest.mark.asyncio
async def test_actual_aggregate_query_and_concurrent_admission(pg_engine,monkeypatch):
    async with AsyncSession(pg_engine) as first,AsyncSession(pg_engine) as second:
        actual=await capacity.aggregate_bytes(first)
        expected=await first.scalar(text('SELECT sum(pg_database_size(oid)) FROM pg_database'))
        assert actual==expected and actual>0
        await first.rollback()
        measured=[]
        async def measured_size(session): measured.append(session); return 699_999_999
        monkeypatch.setattr(capacity,'aggregate_bytes',measured_size)
        await capacity.check_capacity(first)
        with pytest.raises(capacity.HistoryCapacityError,match='another import'):
            await capacity.check_capacity(second)
        assert measured==[first]  # second cannot reuse pre-lock metrics
        await first.commit()
        async def full(_): return 700_000_000
        monkeypatch.setattr(capacity,'aggregate_bytes',full)
        with pytest.raises(capacity.HistoryCapacityError,match='aggregate'):
            await capacity.check_capacity(second)
        await second.rollback()
        # Failed probe/rollback must not leak the advisory lock.
        monkeypatch.setattr(capacity,'aggregate_bytes',measured_size)
        await capacity.check_capacity(first)


@pytest.mark.asyncio
async def test_all_storage_paths_stop_before_metadata_and_live_remains_available(pg_session,monkeypatch):
    from src.jobs.storage import store_match,store_odds,store_market_odds,store_statistics,store_history,store_live
    from src.jobs.verified_odds import fill_only
    session,league_id=pg_session
    async def full(_): return 711_485_004
    monkeypatch.setattr(capacity,'aggregate_bytes',full)
    for _ in range(2):  # repeated direct calls/retries cannot bypass admission
        with pytest.raises(capacity.HistoryCapacityError):
            await store_match(session,league_id,'2025-2026',fixture_data())
    for model in (Match,Team,Season):
        assert await session.scalar(select(func.count()).select_from(model))==0
    match=SimpleNamespace(id=1,kickoff_at=utcnow()-timedelta(days=60))
    for call in (lambda:store_odds(session,match,{},True),lambda:store_market_odds(session,match,{},'corners','full_time'),
                 lambda:store_statistics(session,match,{}),lambda:store_history(session,1,3,[]),
                 lambda:fill_only(session,1,123456,1,{})):
        with pytest.raises(capacity.HistoryCapacityError): await call()
    # An explicit history job cannot masquerade as today's fixture.
    session.info['historical_import']=True
    with pytest.raises(capacity.HistoryCapacityError):
        await store_match(session,league_id,'2026-2027',fixture_data(age=0))
    session.info.clear()
    # Daily discovery and the dedicated score updater do not need history admission.
    live=await store_match(session,league_id,'2026-2027',{**fixture_data(age=0),'status':'scheduled'})
    await store_live(session,live,dict(status='live',live_minute='10',ft_home=1,ft_away=0,raw={}),utcnow())
    assert live.status=='live'


@pytest.mark.asyncio
async def test_paused_work_and_retry_checkpoints_survive_capacity_stop(db,monkeypatch):
    import src.jobs.worker as worker
    from src.jobs.storage import store_match
    monkeypatch.setattr(worker,'database',db)
    async with db.session() as session:
        league=await session.scalar(select(League).where(League.external_id==36))
        match=await store_match(session,league.id,'2025-2026',fixture_data())
        job=ScraperJob(kind='backfill',league_id=league.id,status='queued',current_season='keep',
                       current_round=8,discovery_metrics={'checkpoint':'keep'},processed_matches=12)
        session.add(job);await session.flush()
        item=JobItem(job_id=job.id,match_id=match.id,status='queued',error='preserved retry')
        session.add(item);await session.commit();jid,iid=job.id,item.id
    async def blocked(*args,**kwargs): raise capacity.HistoryCapacityError('Historical import paused: aggregate cap')
    monkeypatch.setattr(worker,'guard_history_write',blocked)
    client=SimpleNamespace()
    await worker.process_item(jid,iid,client)
    async with db.session() as session:
        job=await session.get(ScraperJob,jid);item=await session.get(JobItem,iid)
        assert job.status=='paused_capacity' and job.current_season=='keep' and job.current_round==8
        assert job.processed_matches==12 and job.discovery_metrics=={'checkpoint':'keep'}
        assert item.status=='queued' and item.error=='preserved retry'
        job.status='paused_disabled';await session.commit()
    monkeypatch.setattr(worker,'GoalooClient',lambda:pytest.fail('Paused work must not fetch'))
    await worker.run_job(jid)
    await worker.process_item(jid,iid,client)
    async with db.session() as session:
        job=await session.get(ScraperJob,jid)
        assert job.status=='paused_disabled' and job.processed_matches==12


@pytest.mark.asyncio
async def test_daily_schedule_defers_old_rows_but_keeps_live_discovery(db,monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    async with db.session() as session:
        league=await session.scalar(select(League).where(League.external_id==36))
        job=ScraperJob(kind='update',league_id=league.id,status='running',start_year=2024)
        session.add(job);await session.commit();jid=job.id
    old={**fixture_data(), 'external_league_id':36}
    live={**fixture_data(age=0), 'external_match_id':123457, 'external_league_id':36, 'status':'scheduled'}
    async def seasons(*args,**kwargs): return ['2025-2026']
    async def payload(*args,**kwargs): return {}
    monkeypatch.setattr(worker,'discover_seasons',seasons)
    monkeypatch.setattr(worker,'season_data',payload)
    monkeypatch.setattr(worker,'discover_rounds',lambda p:[1])
    monkeypatch.setattr(worker,'parse_matches',lambda *args:[old,live])
    monkeypatch.setattr(worker,'priority_cycle',AsyncMock())
    actual=worker.store_match
    async def bounded(session,lid,season,data):
        if capacity.historical(data['kickoff_at']):
            raise capacity.HistoryCapacityError('Historical import paused: aggregate cap')
        return await actual(session,lid,season,data)
    monkeypatch.setattr(worker,'store_match',bounded)
    await worker.discover(jid,None)
    async with db.session() as session:
        assert list(await session.scalars(select(Match.external_match_id)))==[123457]
        job=await session.get(ScraperJob,jid)
        assert job.discovery_complete and job.status=='running'
        assert job.discovery_metrics['2025-2026/1']['deferred_capacity']==1
