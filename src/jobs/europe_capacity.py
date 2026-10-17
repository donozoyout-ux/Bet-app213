"""Conservative admission control for supervised European history imports."""
from sqlalchemy import text

# Confirmed Render free plan: fixed 1 GB. Reserve 30% for WAL, temporary files,
# other databases and the continuously running live service. Never resize/delete.
CAPACITY_BYTES = 1_000_000_000
STOP_BYTES = 700_000_000


async def check_capacity(session):
    if session.bind.dialect.name != 'postgresql':
        raise RuntimeError('Historical imports require existing PostgreSQL')
    used = int(await session.scalar(text(
        'SELECT sum(pg_database_size(oid)) FROM pg_database')))
    if used >= STOP_BYTES:
        raise RuntimeError(f'Historical import paused: database bytes {used} >= {STOP_BYTES}')
    return {'database_bytes': used, 'capacity_bytes': CAPACITY_BYTES,
            'stop_bytes': STOP_BYTES}
