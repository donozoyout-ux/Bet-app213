"""Cloudflare R2 access through its S3-compatible API.

Credentials are read only from environment-backed settings and never included in
logs or API responses. Network calls are synchronous; async callers should use
``asyncio.to_thread`` or FastAPI's threadpool.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from urllib.parse import urlsplit, urlunsplit

from src.config import settings

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """A safe-to-report storage error without provider response details."""


def endpoint_hash(endpoint: str, params: dict | None = None) -> str:
    """Stable identifier for a Goaloo endpoint and its request parameters."""
    parts = urlsplit(endpoint)
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, '', ''))
    material = json.dumps({'endpoint': normalized, 'params': params or {}}, sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False, default=str).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def canonical_payload(payload) -> bytes:
    """Serialize JSON consistently so identical payloads share one object key."""
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def raw_object_key(endpoint: str, payload, *, now: datetime | None = None,
                   params: dict | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    digest = hashlib.sha256(canonical_payload(payload)).hexdigest()
    return f'raw/goaloo/{now:%Y/%m/%d}/{endpoint_hash(endpoint, params)}/{digest}.json'


def _client():
    if not settings.r2_endpoint or not settings.r2_bucket or not settings.r2_access_key_id or not settings.r2_secret_access_key:
        raise StorageError('R2 is enabled but its configuration is incomplete')
    try:
        import boto3
    except ImportError as exc:
        raise StorageError('R2 client dependency is unavailable') from exc
    return boto3.client('s3', endpoint_url=settings.r2_endpoint, region_name='auto',
                        aws_access_key_id=settings.r2_access_key_id,
                        aws_secret_access_key=settings.r2_secret_access_key)


def put_raw(payload, endpoint: str, params: dict | None = None) -> str:
    """Write a deterministic raw JSON capture. Repeated payloads use same key."""
    body = canonical_payload(payload)
    key = raw_object_key(endpoint, payload, params=params)
    try:
        # Conditional creation prevents duplicate physical versions on retries.
        _client().put_object(Bucket=settings.r2_bucket, Key=key, Body=body,
                             ContentType='application/json', Metadata={'sha256': hashlib.sha256(body).hexdigest()},
                             IfNoneMatch='*')
    except Exception as exc:
        # An already-existing key is the expected idempotent retry case.
        if getattr(exc, 'response', {}).get('ResponseMetadata', {}).get('HTTPStatusCode') == 412:
            return key
        raise StorageError(f'R2 raw capture failed ({type(exc).__name__})') from None
    return key


def health_check() -> dict:
    """Write/read/hash/delete a unique small probe and report each operation."""
    result = {'r2': 'error', 'bucket': settings.r2_bucket or None,
              'write_test': False, 'read_test': False, 'delete_test': False}
    if not settings.r2_enabled:
        result['r2'] = 'disabled'
        return result
    key = f'_health/{hashlib.sha256(__import__("secrets").token_bytes(32)).hexdigest()}.txt'
    body = b'betapp213-r2-health-v1'
    client = None
    try:
        client = _client()
        client.put_object(Bucket=settings.r2_bucket, Key=key, Body=body, ContentType='text/plain')
        result['write_test'] = True
        response = client.get_object(Bucket=settings.r2_bucket, Key=key)
        read = response['Body'].read()
        result['read_test'] = hashlib.sha256(read).digest() == hashlib.sha256(body).digest()
    except Exception as exc:
        log.warning('R2 health check failed during write/read (%s)', type(exc).__name__)
    finally:
        if client is not None:
            try:
                client.delete_object(Bucket=settings.r2_bucket, Key=key)
                result['delete_test'] = True
            except Exception as exc:
                log.warning('R2 health check cleanup failed (%s)', type(exc).__name__)
    result['r2'] = 'ok' if all(result[k] for k in ('write_test', 'read_test', 'delete_test')) else 'error'
    return result
