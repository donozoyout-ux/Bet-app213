"""Explicit, bounded R2 archival for immutable board editions."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging

import boto3
from botocore.exceptions import ClientError
import zstandard

from src.config import settings

log = logging.getLogger(__name__)


def canonical_json(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def compress(payload: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(payload)


def decompress(payload: bytes) -> bytes:
    return zstandard.ZstdDecompressor().decompress(payload)


def r2_client():
    if not (settings.r2_access_key_id and settings.r2_secret_access_key and settings.r2_endpoint):
        raise RuntimeError('R2 credentials or endpoint are not configured')
    return boto3.client('s3', endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name='auto')


def health_sync():
    enabled = settings.r2_enabled
    bucket_configured = bool(settings.r2_bucket)
    endpoint_configured = bool(settings.r2_endpoint)
    credentials_configured = bool(settings.r2_access_key_id and settings.r2_secret_access_key)
    accessible = False
    if enabled and bucket_configured and endpoint_configured and credentials_configured:
        try:
            r2_client().head_bucket(Bucket=settings.r2_bucket)
            accessible = True
        except Exception as exc:
            log.warning('[R2] health check failed type=%s', type(exc).__name__)
    return {'enabled': enabled, 'bucket_configured': bucket_configured,
        'endpoint_configured': endpoint_configured, 'credentials_configured': credentials_configured,
        'accessible': accessible}


async def health():
    return await asyncio.to_thread(health_sync)


def archive_one_sync(row):
    """Create or validate one object. Never overwrite an existing key."""
    version, league_id, created_at, payload = row
    raw = canonical_json(payload)
    digest = sha256(raw)
    key = f'board-editions/v1/{version}.json.zst'
    manifest_key = f'board-editions/v1/manifests/{version}.json'
    client = r2_client()
    try:
        existing = client.get_object(Bucket=settings.r2_bucket, Key=key)['Body'].read()
    except ClientError as exc:
        code = str(exc.response.get('Error', {}).get('Code', ''))
        if code not in {'NoSuchKey', '404', 'NotFound'}:
            raise
        existing = None

    if existing is not None:
        try:
            existing_raw = decompress(existing)
            existing_digest = sha256(existing_raw)
        except Exception:
            existing_digest = None
        if existing_digest != digest:
            return {'version': version, 'status': 'HASH_CONFLICT'}
        archived_status = 'ALREADY_ARCHIVED'
        compressed = existing
    else:
        compressed = compress(raw)
        try:
            client.put_object(Bucket=settings.r2_bucket, Key=key, Body=compressed,
                ContentType='application/zstd', IfNoneMatch='*')
        except ClientError as exc:
            code = str(exc.response.get('Error', {}).get('Code', ''))
            if code not in {'PreconditionFailed', '412', 'ConditionalRequestConflict'}:
                raise
            existing = client.get_object(Bucket=settings.r2_bucket, Key=key)['Body'].read()
            if sha256(decompress(existing)) != digest:
                return {'version': version, 'status': 'HASH_CONFLICT'}
            compressed = existing
            archived_status = 'ALREADY_ARCHIVED'
        else:
            archived_status = 'ARCHIVED'

    # Read back the bytes from R2 and verify both hash and source payload parity.
    readback = client.get_object(Bucket=settings.r2_bucket, Key=key)['Body'].read()
    readback_raw = decompress(readback)
    readback_digest = sha256(readback_raw)
    if readback_digest != digest or readback_raw != raw:
        return {'version': version, 'status': 'VERIFY_FAILED'}

    manifest = {'schema_version': 1, 'archive_type': 'board-edition', 'version': version,
        'league_id': league_id, 'created_at': created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
        'sha256': digest, 'compressed_size': len(readback), 'uncompressed_size': len(raw),
        'object_key': key, 'archived_at': datetime.now(timezone.utc).isoformat()}
    manifest_bytes = canonical_json(manifest)
    try:
        client.put_object(Bucket=settings.r2_bucket, Key=manifest_key, Body=manifest_bytes,
            ContentType='application/json', IfNoneMatch='*')
    except ClientError as exc:
        code = str(exc.response.get('Error', {}).get('Code', ''))
        if code not in {'PreconditionFailed', '412', 'ConditionalRequestConflict'}:
            raise
        current = client.get_object(Bucket=settings.r2_bucket, Key=manifest_key)['Body'].read()
        try:
            current_manifest = json.loads(current)
        except (ValueError, UnicodeDecodeError):
            return {'version': version, 'status': 'HASH_CONFLICT'}
        if current_manifest.get('sha256') != digest:
            return {'version': version, 'status': 'HASH_CONFLICT'}
    return {'version': version, 'status': archived_status, 'sha256': digest,
        'object_key': key, 'manifest_key': manifest_key,
        'read_back_verified': True, 'postgres_payload_unchanged': True}


async def archive_rows(rows):
    results = []
    # Deliberately sequential and capped by route validation at ten records.
    for row in rows:
        results.append(await asyncio.to_thread(archive_one_sync, row))
    return results
