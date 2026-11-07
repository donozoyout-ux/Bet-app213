"""Immutable, verifiable R2 archives for published board editions.

This module accepts an edition row's values and has no database dependency.
PostgreSQL remains authoritative; callers decide when to read a row and this
module never changes or deletes database data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from src.config import settings
from src.storage import r2

SCHEMA_VERSION = 1
PREFIX = 'board-editions/v1'
_VERSION_RE = re.compile(r'^[a-f0-9]{64}$')


class BoardArchiveError(r2.StorageError):
    """A safe-to-report board archive failure."""


class HashConflict(BoardArchiveError):
    """An immutable archive key already contains different content."""


class VerificationError(BoardArchiveError):
    """An archived object failed integrity or metadata verification."""


def canonical_bytes(payload) -> bytes:
    """Canonical UTF-8 JSON, independent of mapping insertion order."""
    try:
        return r2.canonical_payload(payload)
    except (TypeError, ValueError) as exc:
        raise VerificationError(f'Board payload is not canonical JSON ({type(exc).__name__})') from None


def payload_sha256(payload) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def object_key(version: str) -> str:
    _validate_version(version)
    return f'{PREFIX}/{version}.json.zst'


def manifest_key(version: str) -> str:
    _validate_version(version)
    return f'{PREFIX}/manifests/{version}.json'


def _validate_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise VerificationError('Board edition version must be a 64-character lowercase SHA-256 hex digest')


def _validate_payload(payload) -> None:
    if not isinstance(payload, dict):
        raise VerificationError('Board edition payload must be a JSON object')
    # These are the stable board publication fields produced by publish_board.
    required = {'items', 'availability', 'evaluated', 'engine_analysis', 'coupon_records'}
    if not required.issubset(payload):
        raise VerificationError('Board edition payload is missing required publication fields')
    if not isinstance(payload['items'], list) or not isinstance(payload['coupon_records'], list):
        raise VerificationError('Board edition item collections must be JSON arrays')


def _zstd():
    try:
        import zstandard
    except ImportError:
        raise BoardArchiveError('Zstandard dependency is unavailable') from None
    return zstandard


def _client(client=None):
    if not settings.r2_enabled:
        raise BoardArchiveError('R2 is disabled')
    try:
        return client or r2._client()
    except r2.StorageError as exc:
        raise BoardArchiveError(str(exc)) from None
    except Exception as exc:
        raise BoardArchiveError(f'R2 client initialization failed ({type(exc).__name__})') from None


def _read_object(client, key: str) -> bytes:
    try:
        response = client.get_object(Bucket=settings.r2_bucket, Key=key)
        body = response['Body']
        try:
            return body.read()
        finally:
            close = getattr(body, 'close', None)
            if close:
                close()
    except Exception as exc:
        raise VerificationError(f'R2 object read failed ({type(exc).__name__})') from None


def _read_manifest(client, key: str) -> dict:
    raw = _read_object(client, key)
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError('R2 manifest is not valid UTF-8 JSON') from None
    if not isinstance(value, dict):
        raise VerificationError('R2 manifest must be a JSON object')
    return value


def _conditional_put(client, key: str, body: bytes, content_type: str) -> bool:
    """Create only; return False when an existing object was left untouched."""
    try:
        client.put_object(Bucket=settings.r2_bucket, Key=key, Body=body,
                          ContentType=content_type, IfNoneMatch='*')
        return True
    except Exception as exc:
        status = getattr(exc, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode')
        if status == 412:
            return False
        raise BoardArchiveError(f'R2 conditional write failed ({type(exc).__name__})') from None


def _manifest(version: str, league_id: int, created_at: datetime, digest: str,
              compressed_size: int, uncompressed_size: int, key: str,
              archived_at: datetime) -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'archive_type': 'board_edition',
        'version': version,
        'league_id': int(league_id),
        'created_at': _iso(created_at),
        'sha256': digest,
        'compressed_size': compressed_size,
        'uncompressed_size': uncompressed_size,
        'object_key': key,
        'archived_at': _iso(archived_at),
    }


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _verify_bytes(payload: dict, version: str, league_id: int, created_at: datetime,
                  compressed: bytes, manifest: dict) -> dict:
    _validate_version(version)
    _validate_payload(payload)
    expected = canonical_bytes(payload)
    expected_hash = hashlib.sha256(expected).hexdigest()
    zstandard = _zstd()
    try:
        restored = zstandard.ZstdDecompressor().decompress(compressed)
    except Exception as exc:
        raise VerificationError(f'R2 object decompression failed ({type(exc).__name__})') from None
    if hashlib.sha256(restored).hexdigest() != expected_hash or restored != expected:
        raise VerificationError('R2 object SHA-256 does not match the PostgreSQL payload')
    try:
        parsed = json.loads(restored.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError('R2 object is not valid UTF-8 JSON') from None
    if parsed != payload:
        raise VerificationError('R2 JSON payload differs from the supplied PostgreSQL payload')
    required = {
        'schema_version': SCHEMA_VERSION,
        'archive_type': 'board_edition',
        'version': version,
        'league_id': int(league_id),
        'created_at': _iso(created_at),
        'sha256': expected_hash,
        'compressed_size': len(compressed),
        'uncompressed_size': len(expected),
        'object_key': object_key(version),
    }
    if any(manifest.get(name) != value for name, value in required.items()):
        raise VerificationError('R2 manifest metadata does not match the PostgreSQL edition')
    return {'sha256': expected_hash, 'compressed_size': len(compressed),
            'uncompressed_size': len(expected), 'manifest': manifest}


def archive_board_edition(*, version: str, league_id: int, created_at: datetime,
                          payload, archived_at: datetime | None = None, client=None) -> dict:
    """Conditionally archive one edition, then read it back and verify parity."""
    _validate_version(version)
    _validate_payload(payload)
    client = _client(client)
    zstandard = _zstd()
    raw = canonical_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    compressed = zstandard.ZstdCompressor().compress(raw)
    key = object_key(version)
    mkey = manifest_key(version)
    manifest = _manifest(version, league_id, created_at, digest, len(compressed), len(raw), key,
                         archived_at or datetime.now(timezone.utc))
    created = _conditional_put(client, key, compressed, 'application/zstd')

    # Always verify the object that actually exists. Never overwrite a conflict.
    existing_object = _read_object(client, key)
    try:
        decompressed = zstandard.ZstdDecompressor().decompress(existing_object)
    except Exception as exc:
        raise VerificationError(f'Existing R2 object is corrupted ({type(exc).__name__})') from None
    if hashlib.sha256(decompressed).hexdigest() != digest or decompressed != raw:
        raise HashConflict('Existing object key contains a different payload SHA-256')

    manifest_created = _conditional_put(client, mkey, canonical_bytes(manifest), 'application/json')
    stored_manifest = _read_manifest(client, mkey)
    # archived_at is the first successful archive time and therefore may differ
    # on a retry; all immutable content and source metadata must match.
    verified = _verify_bytes(payload, version, league_id, created_at, existing_object, stored_manifest)
    if not manifest_created:
        if stored_manifest.get('archived_at') is None:
            raise VerificationError('Existing manifest has no archived_at value')
    verified['status'] = 'ARCHIVED' if created and manifest_created else 'ALREADY_ARCHIVED'
    verified['object_key'] = key
    verified['manifest_key'] = mkey
    return verified


def read_archived_board_edition(version: str, *, client=None) -> dict:
    """Read and integrity-check a stored edition using its immutable manifest."""
    _validate_version(version)
    client = _client(client)
    key = object_key(version)
    manifest = _read_manifest(client, manifest_key(version))
    compressed = _read_object(client, key)
    if (manifest.get('schema_version') != SCHEMA_VERSION
            or manifest.get('archive_type') != 'board_edition'
            or manifest.get('object_key') != key
            or manifest.get('version') != version
            or not isinstance(manifest.get('league_id'), int)):
        raise VerificationError('R2 manifest points to a different board edition')
    try:
        raw = _zstd().ZstdDecompressor().decompress(compressed)
        payload = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        raise VerificationError(f'R2 board object is invalid ({type(exc).__name__})') from None
    _validate_payload(payload)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest.get('sha256') or len(raw) != manifest.get('uncompressed_size'):
        raise VerificationError('R2 board object failed manifest SHA-256 or size verification')
    if len(compressed) != manifest.get('compressed_size'):
        raise VerificationError('R2 board object compressed size differs from manifest')
    return payload


def verify_board_edition(*, version: str, league_id: int, created_at: datetime,
                         payload, client=None) -> dict:
    """Verify an archived copy against the PostgreSQL source row values."""
    _validate_version(version)
    client = _client(client)
    key = object_key(version)
    compressed = _read_object(client, key)
    manifest = _read_manifest(client, manifest_key(version))
    return _verify_bytes(payload, version, league_id, created_at, compressed, manifest)


# Keep the service name explicit for callers/readers in future work.
read_board_edition = read_archived_board_edition
