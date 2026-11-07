"""Dry-run or archive exactly one PostgreSQL board edition to R2.

This tool never updates/deletes PostgreSQL rows and has no bulk mode.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings
from src.storage.board_archive import (
    _zstd,
    archive_board_edition,
    canonical_bytes,
    object_key,
    payload_sha256,
)


async def load_edition(version: str):
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is not configured')
    try:
        import asyncpg
    except ImportError:
        raise RuntimeError('asyncpg dependency is unavailable') from None
    try:
        conn = await asyncpg.connect(dsn=settings.database_url, timeout=8,
                                     command_timeout=12, server_settings={'application_name': 'board-edition-archive-readonly'})
    except Exception as exc:
        raise RuntimeError(f'PostgreSQL connection failed ({type(exc).__name__})') from None
    try:
        # asyncpg's read-only transaction blocks accidental SQL mutations.
        async with conn.transaction(readonly=True):
            row = await conn.fetchrow(
                'SELECT version, league_id, created_at, payload '
                'FROM board_editions WHERE version = $1', version)
    except Exception as exc:
        raise RuntimeError(f'PostgreSQL read failed ({type(exc).__name__})') from None
    finally:
        await conn.close()
    if row is None:
        raise RuntimeError('Board edition version was not found')
    payload = row['payload']
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError('PostgreSQL payload is invalid JSON') from None
    return row['version'], row['league_id'], row['created_at'], payload


async def run(version: str, dry_run: bool) -> dict:
    row_version, league_id, created_at, payload = await load_edition(version)
    raw = canonical_bytes(payload)
    compressed = _zstd().ZstdCompressor().compress(raw)
    details = {
        'version': row_version,
        'league_id': league_id,
        'created_at': created_at.isoformat(),
        'sha256': payload_sha256(payload),
        'uncompressed_size': len(raw),
        'compressed_size': len(compressed),
        'object_key': object_key(row_version),
    }
    if dry_run:
        return {'status': 'DRY_RUN', **details}
    return archive_board_edition(version=row_version, league_id=league_id,
                                 created_at=created_at, payload=payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='one 64-character board edition version')
    parser.add_argument('--dry-run', action='store_true', help='read one row and report the archive plan without R2 writes')
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.version, args.dry_run))
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
