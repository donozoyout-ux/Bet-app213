"""Explicit, authenticated first-ten board edition archive pilot."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import undefer
from starlette.concurrency import run_in_threadpool

from src.storage import board_archive as archive

log = logging.getLogger(__name__)
CHECKPOINT_PREFIX = 'board-editions/v1/pilots'
MAX_PILOT_LIMIT = 10


class PilotError(archive.BoardArchiveError):
    """A safe-to-report pilot orchestration error."""


def board_edition_model():
    """Resolve the production ORM model lazily (never run schema setup here)."""
    try:
        from src.models import BoardEdition
    except ImportError:
        raise PilotError('BoardEdition model is unavailable in this application version') from None
    return BoardEdition


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _checkpoint_key(run_id: str) -> str:
    return f'{CHECKPOINT_PREFIX}/{run_id}/checkpoint.json'


def _write_checkpoint(client, bucket: str, key: str, checkpoint: dict, etag: str | None = None) -> str:
    body = json.dumps(checkpoint, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    args = {'Bucket': bucket, 'Key': key, 'Body': body, 'ContentType': 'application/json',
            'Metadata': {'sha256': hashlib.sha256(body).hexdigest()}}
    args['IfNoneMatch' if etag is None else 'IfMatch'] = '*' if etag is None else etag
    try:
        response = client.put_object(**args)
    except Exception as exc:
        code = getattr(exc, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode')
        if code == 412:
            raise PilotError('Pilot checkpoint conditional write conflict') from None
        raise PilotError(f'Pilot checkpoint write failed ({type(exc).__name__})') from None
    new_etag = response.get('ETag')
    if not new_etag:
        raise PilotError('R2 did not return an ETag for the pilot checkpoint')
    return new_etag


async def _load_first_editions(database, model, limit: int) -> list[dict]:
    if not database.configured or not database.ready:
        raise PilotError('PostgreSQL is not ready')
    query = (select(model).options(undefer(model.payload))
             .order_by(model.created_at.asc(), model.version.asc()).limit(limit))
    try:
        async with database.session() as session:
            if session.bind.dialect.name == 'postgresql':
                # Keep the production pilot read-only even if this code changes later.
                await session.execute(text('SET TRANSACTION READ ONLY'))
            rows = list((await session.scalars(query)).all())
            snapshot = [{'version': row.version, 'league_id': int(row.league_id),
                         'created_at': row.created_at, 'payload': row.payload} for row in rows]
            await session.rollback()
            return snapshot
    except PilotError:
        raise
    except Exception as exc:
        raise PilotError(f'PostgreSQL edition read failed ({type(exc).__name__})') from None


async def archive_first_board_editions(database, limit: int, *, model=None) -> dict:
    """Archive at most ten oldest editions; perform no PostgreSQL mutations."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PILOT_LIMIT:
        raise ValueError('Pilot limit must be between 1 and 10')
    # Validate R2 configuration before reading database payloads.
    client = archive._client()
    model = model or board_edition_model()
    editions = await _load_first_editions(database, model, limit)
    run_id = uuid.uuid4().hex
    key = _checkpoint_key(run_id)
    planned = [{
        'version': row['version'], 'league_id': row['league_id'],
        'created_at': _iso(row['created_at']),
        'sha256': archive.payload_sha256(row['payload']),
        'object_key': archive.object_key(row['version']),
    } for row in editions]
    plan_bytes = json.dumps(planned, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    checkpoint = {
        'schema_version': 1,
        'archive_type': 'board_edition_pilot',
        'run_id': run_id,
        'started_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'plan_sha256': hashlib.sha256(plan_bytes).hexdigest(),
        'planned': len(editions),
        'processed': 0,
        'archived': 0,
        'already_archived': 0,
        'verified': 0,
        'failed': 0,
        'results': [],
        'complete': False,
    }
    etag = await run_in_threadpool(_write_checkpoint, client, archive.settings.r2_bucket, key, checkpoint)
    results = []
    for row in editions:
        result = {'version': row['version'], 'league_id': row['league_id'],
                  'sha256': archive.payload_sha256(row['payload']),
                  'object_key': archive.object_key(row['version']), 'status': 'FAILED',
                  'verified': False}
        try:
            stored = await run_in_threadpool(
                archive.archive_board_edition,
                version=row['version'], league_id=row['league_id'],
                created_at=row['created_at'], payload=row['payload'], client=client)
            result.update(status=stored['status'], verified=True,
                          compressed_size=stored['compressed_size'],
                          uncompressed_size=stored['uncompressed_size'],
                          manifest_key=stored['manifest_key'])
            checkpoint['archived' if stored['status'] == 'ARCHIVED' else 'already_archived'] += 1
            checkpoint['verified'] += 1
        except Exception as exc:
            result['error_type'] = type(exc).__name__
            checkpoint['failed'] += 1
            log.warning('Board edition pilot item failed version=%s error=%s',
                        row['version'], type(exc).__name__)
        results.append(result)
        checkpoint['processed'] += 1
        checkpoint['results'] = results
        try:
            etag = await run_in_threadpool(_write_checkpoint, client, archive.settings.r2_bucket,
                                           key, checkpoint, etag)
        except Exception as exc:
            # Do not continue if durable progress cannot be recorded.
            checkpoint['checkpoint_error_type'] = type(exc).__name__
            checkpoint['complete'] = False
            return {**checkpoint, 'checkpoint_key': key,
                    'checkpoint_saved': False, 'results': results}

    checkpoint['complete'] = checkpoint['processed'] == checkpoint['planned'] and checkpoint['failed'] == 0
    checkpoint['finished_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    try:
        etag = await run_in_threadpool(_write_checkpoint, client, archive.settings.r2_bucket,
                                       key, checkpoint, etag)
    except Exception as exc:
        checkpoint['checkpoint_error_type'] = type(exc).__name__
        checkpoint['complete'] = False
        return {**checkpoint, 'checkpoint_key': key, 'checkpoint_saved': False,
                'results': results}
    return {**checkpoint, 'checkpoint_key': key, 'checkpoint_saved': True,
            'results': results}
