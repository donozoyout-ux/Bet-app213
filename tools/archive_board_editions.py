"""Resumable archive copy of every board_editions payload to R2.

Default mode is a PostgreSQL-only dry run. R2 writes require the explicit
combination ``--all --execute``. PostgreSQL is accessed through read-only
transactions; this tool has no cleanup or delete mode.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings
from src.storage import r2
from src.storage import board_archive as archive

DEFAULT_CHECKPOINT = Path('.board-editions-archive-v1.checkpoint.json')


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _checkpoint_plan_path(checkpoint: Path) -> Path:
    return checkpoint.with_suffix('.plan.json')


def _plan_sha256(editions: list[dict]) -> str:
    body = json.dumps(editions, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(body).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


def _write_checkpoint(path: Path, state: dict) -> None:
    _atomic_json(path, state)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f'Cannot read migration state file: {path}') from None
    if not isinstance(value, dict):
        raise RuntimeError(f'Migration state file is invalid: {path}')
    return value


async def connect_database():
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is not configured')
    try:
        import asyncpg
    except ImportError:
        raise RuntimeError('asyncpg dependency is unavailable') from None
    try:
        return await asyncpg.connect(
            dsn=settings.database_url,
            timeout=8,
            command_timeout=30,
            server_settings={'application_name': 'board-editions-r2-archive-readonly'},
        )
    except Exception as exc:
        raise RuntimeError(f'PostgreSQL connection failed ({type(exc).__name__})') from None


async def snapshot_edition_list(conn) -> tuple[str, list[dict]]:
    """Capture the exact edition set once so new rows cannot join a running job."""
    try:
        async with conn.transaction(readonly=True):
            database_name = await conn.fetchval('SELECT current_database()')
            rows = await conn.fetch(
                'SELECT version, league_id, created_at '
                'FROM board_editions ORDER BY created_at, version'
            )
    except Exception as exc:
        raise RuntimeError(f'PostgreSQL inventory read failed ({type(exc).__name__})') from None
    plan = []
    seen = set()
    for row in rows:
        version = row['version']
        if version in seen:
            raise RuntimeError('Production inventory contains a duplicate edition version')
        seen.add(version)
        archive.object_key(version)  # validates version before any R2 action
        plan.append({'version': version, 'league_id': int(row['league_id']),
                     'created_at': _timestamp(row['created_at'])})
    return database_name, plan


async def fetch_payload_batch(conn, versions: list[str]) -> dict[str, dict]:
    """Fetch a bounded batch of source payloads inside a read-only transaction."""
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(
                'SELECT version, league_id, created_at, payload '
                'FROM board_editions WHERE version = ANY($1::varchar[])', versions
            )
    except Exception as exc:
        raise RuntimeError(f'PostgreSQL payload read failed ({type(exc).__name__})') from None
    result = {}
    for row in rows:
        payload = row['payload']
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                raise RuntimeError(f'Edition payload is invalid JSON: {row["version"]}') from None
        result[row['version']] = {
            'version': row['version'],
            'league_id': int(row['league_id']),
            'created_at': row['created_at'],
            'payload': payload,
        }
    return result


async def process_plan(*, plan: list[dict], fetch_batch: Callable,
                       mode: str, client=None, start_at: int = 0,
                       limit: int | None = None, batch_size: int = 25,
                       checkpoint: Callable | None = None,
                       progress: Callable | None = None) -> dict:
    """Run a bounded dry-run, verified archive pass, or read-only verification."""
    if mode not in {'dry-run', 'execute', 'verify-only'}:
        raise ValueError('Invalid archive mode')
    if not 1 <= batch_size <= 100:
        raise ValueError('batch_size must be between 1 and 100')
    if start_at < 0 or start_at > len(plan):
        raise ValueError('Invalid resume position')
    stop_at = len(plan) if limit is None else min(len(plan), start_at + limit)
    summary = {'mode': mode, 'selected': len(plan), 'processed': 0,
               'archived': 0, 'already_archived': 0, 'verified': 0,
               'uncompressed_bytes': 0, 'compressed_bytes': 0,
               'complete': stop_at == len(plan)}

    for offset in range(start_at, stop_at, batch_size):
        batch = plan[offset:min(stop_at, offset + batch_size)]
        rows = await fetch_batch([entry['version'] for entry in batch])
        if set(rows) != {entry['version'] for entry in batch}:
            raise RuntimeError('PostgreSQL edition batch did not match the captured inventory')
        for batch_index, entry in enumerate(batch):
            row = rows[entry['version']]
            if (row['league_id'] != entry['league_id']
                    or _timestamp(row['created_at']) != entry['created_at']):
                raise RuntimeError(f'Edition metadata changed after inventory capture: {entry["version"]}')
            archive._validate_payload(row['payload'])
            raw = archive.canonical_bytes(row['payload'])
            compressed = archive._zstd().ZstdCompressor().compress(raw)
            summary['uncompressed_bytes'] += len(raw)
            summary['compressed_bytes'] += len(compressed)

            if mode == 'execute':
                result = archive.archive_board_edition(
                    version=row['version'], league_id=row['league_id'],
                    created_at=row['created_at'], payload=row['payload'], client=client)
                if result['status'] == 'ARCHIVED':
                    summary['archived'] += 1
                else:
                    summary['already_archived'] += 1
            elif mode == 'verify-only':
                archive.verify_board_edition(
                    version=row['version'], league_id=row['league_id'],
                    created_at=row['created_at'], payload=row['payload'], client=client)
                summary['verified'] += 1

            summary['processed'] += 1
            if checkpoint is not None:
                checkpoint(offset + batch_index + 1, summary, entry['version'])
            if progress is not None and (summary['processed'] % 100 == 0 or summary['processed'] == stop_at - start_at):
                progress(summary, entry['version'])
    return summary


async def _run(args) -> dict:
    conn = await connect_database()
    try:
        checkpoint_path = Path(args.checkpoint)
        plan_path = _checkpoint_plan_path(checkpoint_path)
        mode = 'execute' if args.execute else 'verify-only' if args.verify_only else 'dry-run'
        client = archive._client() if mode in {'execute', 'verify-only'} else None
        if args.resume:
            state = _read_json(checkpoint_path)
            saved_plan = _read_json(plan_path)
            async with conn.transaction(readonly=True):
                database_name = await conn.fetchval('SELECT current_database()')
            if state.get('database') != database_name or state.get('bucket') != settings.r2_bucket:
                raise RuntimeError('Checkpoint database or R2 bucket does not match this environment')
            plan = saved_plan.get('editions')
            if (saved_plan.get('database') != database_name
                    or saved_plan.get('bucket') != settings.r2_bucket
                    or not isinstance(plan, list)
                    or len(plan) != args.expected_count
                    or saved_plan.get('edition_count') != len(plan)
                    or saved_plan.get('plan_sha256') != _plan_sha256(plan)
                    or state.get('plan_sha256') != saved_plan.get('plan_sha256')):
                raise RuntimeError('Saved archive plan does not match this environment')
            for item in plan:
                archive.object_key(item.get('version'))
                if not isinstance(item.get('league_id'), int) or not isinstance(item.get('created_at'), str):
                    raise RuntimeError('Saved archive plan contains invalid edition metadata')
            start_at = int(state.get('next_index', -1))
            if start_at < 0 or start_at > len(plan):
                raise RuntimeError('Archive checkpoint position is invalid')
            expected_last = plan[start_at - 1]['version'] if start_at else None
            if state.get('last_version') != expected_last or bool(state.get('complete')) != (start_at == len(plan)):
                raise RuntimeError('Archive checkpoint cursor does not match the saved plan')
        else:
            if args.execute and (checkpoint_path.exists() or plan_path.exists()):
                raise RuntimeError('Checkpoint/plan already exists; use --resume or choose another --checkpoint')
            database_name, plan = await snapshot_edition_list(conn)
            start_at = 0
        if len(plan) != args.expected_count:
            raise RuntimeError(
                f'Inventory count is {len(plan)}; expected {args.expected_count}. '
                'Review the production inventory before proceeding.'
            )

        if not args.resume and args.execute:
            plan_digest = _plan_sha256(plan)
            saved_plan = {'schema_version': 1, 'archive_type': 'board_editions_full_copy',
                          'database': database_name, 'bucket': settings.r2_bucket,
                          'created_at': datetime.now(timezone.utc).isoformat(),
                          'edition_count': len(plan), 'plan_sha256': plan_digest, 'editions': plan}
            _atomic_json(plan_path, saved_plan)
            state = {'schema_version': 1, 'database': database_name,
                     'bucket': settings.r2_bucket, 'next_index': 0,
                     'archived_total': 0, 'already_archived_total': 0,
                     'plan_sha256': plan_digest, 'complete': False}
            _write_checkpoint(checkpoint_path, state)

        invocation_archived = 0
        invocation_existing = 0
        def save_progress(next_index, summary, last_version):
            nonlocal invocation_archived, invocation_existing
            state.update({'next_index': next_index,
                          'archived_total': state.get('archived_total', 0) +
                          summary['archived'] - invocation_archived,
                          'already_archived_total': state.get('already_archived_total', 0) +
                          summary['already_archived'] - invocation_existing,
                          'last_version': last_version,
                          'complete': next_index == len(plan)})
            invocation_archived = summary['archived']
            invocation_existing = summary['already_archived']
            _write_checkpoint(checkpoint_path, state)

        if args.resume and state.get('complete'):
            return {'mode': mode, 'selected': len(plan), 'processed': 0,
                    'archived': 0, 'already_archived': 0, 'verified': 0,
                    'uncompressed_bytes': 0, 'compressed_bytes': 0, 'complete': True,
                    'message': 'Checkpoint is already complete'}

        return await process_plan(
            plan=plan, fetch_batch=lambda versions: fetch_payload_batch(conn, versions),
            mode=mode, client=client, start_at=start_at,
            limit=args.limit, batch_size=args.batch_size,
            checkpoint=save_progress if mode == 'execute' else None,
            progress=lambda summary, version: print(json.dumps({
                'progress': start_at + summary['processed'], 'selected': summary['selected'],
                'last_version': version, 'mode': mode,
            }, sort_keys=True)))
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', required=True,
                        help='explicitly confirm the complete captured board_editions set')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--execute', action='store_true',
                       help='write and verify archives in R2; never changes PostgreSQL')
    modes.add_argument('--verify-only', action='store_true',
                       help='read and verify every selected R2 archive without writing')
    parser.add_argument('--resume', action='store_true', help='resume an existing --execute checkpoint')
    parser.add_argument('--checkpoint', default=str(DEFAULT_CHECKPOINT),
                        help='local checkpoint path for --execute/--resume')
    parser.add_argument('--batch-size', type=int, default=25,
                        help='bounded PostgreSQL read batch size (1..100)')
    parser.add_argument('--expected-count', type=int, default=4601,
                        help='fail closed unless the captured inventory has this exact row count (default: 4601)')
    parser.add_argument('--limit', type=int, help='process at most this many rows in this invocation')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if args.expected_count < 1:
        parser.error('--expected-count must be positive')
    if args.resume and not args.execute:
        parser.error('--resume requires --execute')
    if args.verify_only and args.resume:
        parser.error('--resume is only valid with --execute')
    try:
        summary = asyncio.run(_run(args))
    except Exception as exc:
        # Report only safe exception class and application-level message.
        print(json.dumps({'status': 'ERROR', 'error_type': type(exc).__name__,
                          'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
