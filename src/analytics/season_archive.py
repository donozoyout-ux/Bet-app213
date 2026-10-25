"""Allowlisted season totals only; no IDs, fixture rows, names or odds on disk.

No network requests to GitHub. The database remains the recoverable source of
truth. These files describe coverage; they never enter the per-match predictor.
"""
import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, case
from src.models import Match, Season, utcnow
from .history_cache import enabled, aware

SEASONS = ('2024-2025', '2025-2026', '2026-2027')
SCHEMA_VERSION = '1'
MAX_BYTES = 8192
MAX_AGE = timedelta(minutes=10)
COUNTS = ('source_matches', 'finished_matches', 'scored_matches', 'home_goals', 'away_goals')
FIELDS = ('schema_version', 'season', 'generated_utc', 'source_scope', 'source_updated_utc',
          'content_hash', *COUNTS)
log = logging.getLogger(__name__)


def directory():
    return Path(os.getenv('SEASON_AGGREGATE_DIR', 'data/aggregates'))


def scope(engine):
    url = engine.url
    # Bind cached coverage to the selected database, without storing credentials.
    identity = (url.drivername, url.host, url.port, url.database)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def digest(row):
    # Generation time is not content: repeating a generation is idempotent.
    content = {k: row[k] for k in FIELDS if k not in ('generated_utc', 'content_hash')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


async def aggregate_rows(session, now=None):
    """One GROUP BY with <=3 result rows, never materialize source fixtures."""
    now = now or utcnow()
    aliases = {season: (season, season.replace('-', '/'), season[:5] + season[-2:]) for season in SEASONS}
    season_key = case(*[(Season.season_name.in_(values), key) for key, values in aliases.items()])
    finished = Match.status == 'finished'
    scored = finished & Match.ft_home.is_not(None) & Match.ft_away.is_not(None) & (Match.ft_home >= 0) & (Match.ft_away >= 0)
    query = select(season_key.label('season'), func.count(Match.id),
        func.sum(case((finished, 1), else_=0)), func.sum(case((scored, 1), else_=0)),
        func.sum(case((scored, Match.ft_home), else_=0)), func.sum(case((scored, Match.ft_away), else_=0)),
        func.max(Match.updated_at)).join(Season, Season.id == Match.season_id).where(
            Season.season_name.in_([v for values in aliases.values() for v in values])).group_by(season_key)
    found = {row[0]: row[1:] for row in await session.execute(query)}
    output = []
    for season in SEASONS:
        values = found.get(season, (0, 0, 0, 0, 0, None))
        row = dict(schema_version=SCHEMA_VERSION, season=season, generated_utc=now.isoformat(),
                   source_scope=scope(session.bind), source_updated_utc=aware(values[5]).isoformat() if values[5] else '')
        row.update({key: str(value or 0) for key, value in zip(COUNTS, values[:5])})
        row['content_hash'] = digest(row)
        output.append(row)
    return output


def read_one(root, season, expected_scope, now=None, fresh=True):
    if season not in SEASONS:
        raise ValueError('Unsupported season')
    path = Path(root) / f'season-{season}.csv'
    # A bounded read also catches files which grew after stat().
    with path.open('rb') as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Oversized aggregate')
    reader = csv.DictReader(io.StringIO(data.decode('utf-8')))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError('Unapproved aggregate schema')
    rows = list(reader)
    if len(rows) != 1 or set(rows[0]) != set(FIELDS):
        raise ValueError('Invalid aggregate row count')
    row = rows[0]
    return validate_row(row, season, expected_scope, now, fresh)


def validate_row(row, season, expected_scope, now=None, fresh=True):
    if set(row) != set(FIELDS) or any(not isinstance(v, str) for v in row.values()):
        raise ValueError('Unapproved aggregate schema')
    if row['schema_version'] != SCHEMA_VERSION or row['season'] != season or row['source_scope'] != expected_scope:
        raise ValueError('Aggregate version or database mismatch')
    if row['content_hash'] != digest(row):
        raise ValueError('Aggregate checksum mismatch')
    for key in COUNTS:
        if not row[key].isdigit() or len(row[key]) > 16:
            raise ValueError('Invalid coverage count')
    if not int(row['scored_matches']) <= int(row['finished_matches']) <= int(row['source_matches']):
        raise ValueError('Inconsistent coverage')
    generated = datetime.fromisoformat(row['generated_utc'])
    if generated.tzinfo is None:
        raise ValueError('Missing generation timezone')
    if row['source_updated_utc']:
        updated = datetime.fromisoformat(row['source_updated_utc'])
        if updated.tzinfo is None:
            raise ValueError('Missing observation timezone')
    now = now or utcnow()
    if fresh and not timedelta(0) <= now - generated <= MAX_AGE:
        raise ValueError('Aggregate expired')
    return row


def publish(root, rows):
    """One atomic replace per self-contained season file; safe on read-only disks.

    Callers own error policy. No half CSV/sidecar pair can be observed by readers.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for row in sorted(rows, key=lambda r: r['season']):
        if row.get('season') not in SEASONS:
            raise ValueError('Unapproved export')
        validate_row(row, row['season'], row.get('source_scope'), fresh=False)
        path = root / f"season-{row['season']}.csv"
        try:
            old = read_one(root, row['season'], row['source_scope'], fresh=False)
            if old == row:
                continue
        except (OSError, ValueError, UnicodeError, csv.Error, TypeError):
            pass
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader(); writer.writerow(row)
        data = output.getvalue().encode()
        if len(data) > MAX_BYTES:
            raise ValueError('Oversized export')
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix='.season-', delete=False) as handle:
                tmp = handle.name
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)


async def read_or_query(session, now=None):
    """Cache miss/corruption/restart falls back to <=3 aggregate rows from SQL."""
    if not enabled():
        return {'enabled': False, 'source': 'disabled', 'seasons': []}
    now = now or utcnow()
    try:
        rows = await asyncio.to_thread(lambda: [read_one(directory(), s, scope(session.bind), now) for s in SEASONS])
        source = 'local_csv'
    except (OSError, ValueError, UnicodeError, csv.Error, TypeError):
        rows = await aggregate_rows(session, now)
        source = 'postgresql_aggregate'
    return {'enabled': True, 'source': source, 'seasons': rows}


async def refresh(db, now=None):
    if not enabled():
        return False
    async with db.session() as session:
        rows = await aggregate_rows(session, now)
    try:
        await asyncio.to_thread(publish, directory(), rows)
    except OSError as exc:
        log.warning('[HISTORY] aggregate files unavailable error=%s', type(exc).__name__)
        return False  # Immutable deployment: SQL remains available.
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build'])
    parser.parse_args()
    from src.db import database
    if not enabled() or not database.configured:
        parser.error('Set HYBRID_HISTORY_ENABLED=true and DATABASE_URL first')
    async def run():
        try:
            # No initialize(): this command must never run DDL or seed mutations.
            if not await refresh(database):
                raise SystemExit(1)
        finally:
            await database.close()
    asyncio.run(run())


if __name__ == '__main__':
    main()
