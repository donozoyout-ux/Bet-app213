"""Fresh aggregate PostgreSQL admission shared by every history writer."""
import asyncio
import time
from datetime import timedelta, timezone
from sqlalchemy import text

# Confirmed Render free plan: fixed 1 GB. Reserve 30% for WAL, temporary files,
# other databases and the continuously running live service. Never resize/delete.
CAPACITY_BYTES = 1_000_000_000
STOP_BYTES = 700_000_000
IMPORT_LOCK = 213102
PROBE_MAX_SECONDS = 2.0


class HistoryCapacityError(RuntimeError):
    """A resumable capacity stop, never a source/parser failure."""


async def aggregate_bytes(session):
    return await session.scalar(text('WITH sizes AS MATERIALIZED '
        '(SELECT pg_database_size(oid) AS bytes FROM pg_database) '
        'SELECT CASE WHEN count(*)=count(bytes) THEN sum(bytes) END FROM sizes'))


async def check_capacity(session):
    if session.bind.dialect.name != 'postgresql':
        raise HistoryCapacityError('Historical imports require existing PostgreSQL')
    started = time.monotonic()
    try:
        # No cached results. Serialize history transactions, but never wait behind
        # another importer or take a lock used by live results/API readers.
        with session.no_autoflush:
            async with asyncio.timeout(PROBE_MAX_SECONDS):
                locked = await session.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': IMPORT_LOCK})
                if locked is not True:
                    raise HistoryCapacityError('Historical import paused: another import transaction is active')
                used = await aggregate_bytes(session)
        if time.monotonic() - started >= PROBE_MAX_SECONDS:
            raise HistoryCapacityError('Historical import paused: stale capacity measurement')
        # SUM(bigint) is Decimal in asyncpg. Never accept NULL, a partial sum,
        # nonintegral/negative values or a string interpreted as a size.
        from decimal import Decimal
        if type(used) not in (int, Decimal) or used <= 0 or used != int(used):
            raise HistoryCapacityError('Historical import paused: unavailable capacity measurement')
        used = int(used)
    except HistoryCapacityError:
        raise
    except Exception:
        raise HistoryCapacityError('Historical import paused: capacity probe unavailable') from None
    if used >= STOP_BYTES:
        raise HistoryCapacityError(f'Historical import paused: aggregate database bytes {used} >= {STOP_BYTES}')
    return {'database_bytes': used, 'capacity_bytes': CAPACITY_BYTES,
            'stop_bytes': STOP_BYTES}


def historical(kickoff):
    from src.models import utcnow
    if kickoff is not None and kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff is None or kickoff < utcnow()-timedelta(days=7)


async def guard_history_write(session, kickoff=None, *, force=False):
    """Protect storage even when called by update/startup/retry/manual tools.

    The existing live updater uses a seven-day correction window. Its writes
    remain available; explicitly historical jobs are guarded even in that window.
    Missing dates are never treated as a live exemption. SQLite is test-only
    (Database rejects it in production); no production metric is inferred from it.
    """
    if session.bind.dialect.name == 'sqlite':
        return
    if force or session.info.get('historical_import') or historical(kickoff):
        await check_capacity(session)
