"""Independent bounded settlement checkpoints; no provider or message calls."""
import asyncio
import logging
from datetime import timedelta
from sqlalchemy import text
from src.models import WorkerCheckpoint, utcnow
from src.jobs.worker import aware
from src.analytics.settlements import settle_batch

log = logging.getLogger(__name__)
LOCK_ID = 213014
INTERVAL = 60


async def sweep(db, now=None):
    controlled_clock = now is not None
    now = now or utcnow()
    async with db.session() as session:
        if session.bind.dialect.name == 'postgresql':
            if not await session.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':LOCK_ID}):
                return False
        checkpoint = await session.get(WorkerCheckpoint, 'settlement')
        if checkpoint and aware(checkpoint.next_attempt_at) > now:
            return False
        if checkpoint is None:
            checkpoint = WorkerCheckpoint(name='settlement', cursor=0, failures=0, attempted_at=now, next_attempt_at=now)
            session.add(checkpoint)
        # A failed batch never advances the durable cursor or partially commits.
        cursor = await settle_batch(session, after_match_id=checkpoint.cursor, limit=100, only_unsettled=True)
        batch = session.info['settlement_batch']
        checkpoint.cursor = cursor if batch['examined'] == 100 else 0
        checkpoint.attempted_at = now
        checkpoint.last_success_at = now if controlled_clock else utcnow()
        checkpoint.next_attempt_at = checkpoint.last_success_at + timedelta(seconds=INTERVAL)
        checkpoint.examined = batch['examined']; checkpoint.written = batch['written']
        checkpoint.failures = 0; checkpoint.error_type = None
        await session.commit()
        log.info('[SETTLEMENT] sweep_ok examined=%s written=%s', batch['examined'], batch['written'])
        return True


async def record_failure(db, exc, now=None):
    now = now or utcnow()
    async with db.session() as session:
        if session.bind.dialect.name == 'postgresql':
            if not await session.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':LOCK_ID}):
                return
        row = await session.get(WorkerCheckpoint, 'settlement')
        if row is None:
            row = WorkerCheckpoint(name='settlement', cursor=0, failures=0, attempted_at=now, next_attempt_at=now)
            session.add(row)
        row.failures += 1; row.attempted_at = now
        row.error_type = type(exc).__name__[:80]
        row.next_attempt_at = now + timedelta(seconds=min(60, 5*2**min(row.failures,4)))
        await session.commit()


async def loop(db):
    while True:
        try:
            async with asyncio.timeout(20):
                await sweep(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('[SETTLEMENT] sweep_failed error=%s', type(exc).__name__)
            try:
                async with asyncio.timeout(5): await record_failure(db, exc)
            except asyncio.CancelledError: raise
            except Exception:
                log.warning('[SETTLEMENT] checkpoint unavailable')
        await asyncio.sleep(5)  # Durable next_attempt_at controls cadence/backoff.


async def health(session, now=None):
    now = now or utcnow()
    row = await session.get(WorkerCheckpoint, 'settlement')
    if row is None:
        return dict(heartbeat='warming', interval_seconds=INTERVAL, last_success_at=None,
            attempted_at=None, next_attempt_at=None, failures=0, error_type=None)
    status = 'retrying' if row.failures else 'ok' if row.last_success_at and (
        0<=(now-aware(row.last_success_at)).total_seconds()<=3*INTERVAL) else 'stale'
    return dict(heartbeat=status, interval_seconds=INTERVAL, attempted_at=aware(row.attempted_at),
        last_success_at=aware(row.last_success_at), next_attempt_at=aware(row.next_attempt_at),
        failures=row.failures, error_type=row.error_type, cursor=row.cursor,
        examined=row.examined, written=row.written)
