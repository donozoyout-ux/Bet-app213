from datetime import timedelta
from dataclasses import replace
from sqlalchemy import select
from src.models import League, ScraperJob, JobItem, utcnow
from tests.test_jobs import install_source
import pytest


def test_albania_observation_is_explicit_and_strictly_bounded():
    from scripts.europe_bounded_history import pilot_records
    assert {r['external_id'] for r in pilot_records([315,348],5)} == {315,348}
    for ids,limit in [([315],6),([348],25),([7],5),([315,315],5),([],5)]:
        with pytest.raises(ValueError):
            pilot_records(ids,limit)


@pytest.mark.asyncio
async def test_supervised_import_stops_when_live_api_times_out(monkeypatch):
    import httpx
    import scripts.europe_bounded_history as importer
    monkeypatch.setenv('EUROPE_API_GUARD_URL','https://bet-app213.onrender.com/health')
    class SlowAPI:
        def __init__(self, timeout): assert timeout == 5
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get(self,url): raise httpx.ReadTimeout('unavailable')
    monkeypatch.setattr(importer.httpx,'AsyncClient',SlowAPI)
    with pytest.raises(RuntimeError,match='paused'):
        await importer.check_api_health()


@pytest.mark.asyncio
async def test_postgres_lock_owner_is_not_recovered_by_competitor(monkeypatch):
    import os
    from src.db import Database
    import src.jobs.worker as worker
    from sqlalchemy import text
    if not os.getenv('TEST_POSTGRES_URL'):
        pytest.skip('TEST_POSTGRES_URL is not configured')
    db = Database(os.environ['TEST_POSTGRES_URL'])
    db.ready = True
    monkeypatch.setattr(worker,'database',db)
    monkeypatch.setattr(worker,'settings',replace(worker.settings,auto_backfill_on_empty=False))
    async def no_op(*args): pass
    monkeypatch.setattr(worker,'enqueue_initial_backfill',no_op)
    monkeypatch.setattr(worker,'enqueue_catalog_backfills',no_op)
    async def forbidden(*args):
        raise AssertionError('A competing worker must not recover active work')
    monkeypatch.setattr(worker,'reconcile_owned_queue',forbidden)
    try:
        async with db.engine.connect() as owner:
            await owner.execute(text('SELECT pg_advisory_lock(:id)'),{'id':worker.LOCK_ID})
            await owner.commit()
            try:
                await worker.work(once=True)
            finally:
                await owner.execute(text('SELECT pg_advisory_unlock(:id)'),{'id':worker.LOCK_ID})
                await owner.commit()
    finally:
        await db.close()


async def test_recovery_preserves_checkpoint_errors_and_completed_items(db, monkeypatch):
    worker = await install_source(monkeypatch, db)
    job = await worker.enqueue('backfill')
    await worker.discover(job.id, None)
    async with db.session() as session:
        stored = await session.get(ScraperJob, job.id)
        started = utcnow()-timedelta(days=9)
        stored.status, stored.started_at = 'running', started
        stored.processed_matches, stored.failed_matches = 1, 1
        stored.last_error = 'keep source evidence'
        items = (await session.scalars(select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.id))).all()
        items[0].status = 'completed'
        items[1].status, items[1].error = 'failed', 'source unavailable'
        items[2].status = 'running'
        league = await session.get(League, stored.league_id)
        league.enabled = False
        await session.commit()
        await worker.reconcile_owned_queue(session)
        assert stored.status == 'paused_disabled'
        assert stored.started_at == started
        assert stored.discovery_complete
        assert (stored.processed_matches, stored.failed_matches, stored.last_error) == (1, 1, 'keep source evidence')
        assert [i.status for i in items[:3]] == ['completed','failed','queued']
        assert items[1].error == 'source unavailable'
        league.enabled = True
        await session.commit()
        await worker.reconcile_owned_queue(session)
        assert stored.status == 'queued'
        assert items[0].status == 'completed'


async def test_round_robin_keeps_updates_first_and_manual_work_separate(db, monkeypatch):
    worker = await install_source(monkeypatch, db)
    async with db.session() as session:
        league = await session.scalar(select(League).where(League.external_id == 36))
        old = ScraperJob(league_id=league.id, kind='stats_backfill',status='running',priority=1,
                         checkpoint_at=utcnow())
        other = ScraperJob(league_id=league.id,kind='backfill',status='queued',priority=100)
        manual = ScraperJob(league_id=league.id,kind='europe_bounded',status='running',priority=0)
        daily = ScraperJob(league_id=league.id,kind='update',status='queued',priority=999,checkpoint_at=utcnow())
        session.add_all([old,other,manual,daily]); await session.commit()
        await worker.reconcile_owned_queue(session)
        assert manual.status == 'running'  # Independent lock owner, never adopted.
        assert await session.scalar(worker.next_job_query()) == daily.id
        daily.status = 'completed';await session.commit()
        assert await session.scalar(worker.next_job_query()) == other.id
        other.checkpoint_at = utcnow()+timedelta(seconds=1);await session.commit()
        assert await session.scalar(worker.next_job_query()) == old.id


async def test_history_slice_yields_and_resumes_without_replaying_completed(db, monkeypatch):
    worker = await install_source(monkeypatch, db)
    monkeypatch.setattr(worker,'settings',replace(worker.settings,auto_backfill_on_empty=False))
    monkeypatch.setattr(worker,'HISTORY_SLICE_SIZE',3)
    async def no_priority(*args): pass
    monkeypatch.setattr(worker,'priority_cycle',no_priority)
    job = await worker.enqueue('backfill')
    await worker.run_job(job.id)
    async with db.session() as session:
        stored = await session.get(ScraperJob,job.id)
        assert stored.status == 'queued' and stored.processed_matches == 3
        assert stored.checkpoint_at is not None
        completed = set(await session.scalars(select(JobItem.id).where(JobItem.job_id==job.id,JobItem.status=='completed')))
    calls=[]
    original=worker.process_item
    async def record(job_id,item_id,client):
        calls.append(item_id)
        await original(job_id,item_id,client)
    monkeypatch.setattr(worker,'process_item',record)
    await worker.run_job(job.id)
    assert len(calls)==3 and not completed.intersection(calls)
    async with db.session() as session:
        assert (await session.get(ScraperJob,job.id)).processed_matches == 6
