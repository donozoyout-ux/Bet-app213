"""Explicit, bounded R2 archival for immutable board editions."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from urllib.parse import urlsplit

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


def endpoint_format_valid(endpoint: str) -> bool:
    """Accept only the account-level HTTPS endpoint, without a bucket/path."""
    return endpoint_format_issue(endpoint) is None


def endpoint_format_issue(endpoint: str) -> str | None:
    """Return a value-free diagnostic for malformed endpoint configuration."""
    if not endpoint:
        return 'missing'
    # urllib.parse strips some leading C0 whitespace; botocore rejects tabs/CR/LF.
    if endpoint != endpoint.strip() or any(char in endpoint for char in '\t\r\n'):
        return 'control_whitespace'
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme != 'https':
            return 'https_required'
        if not re.fullmatch(r'[a-f0-9]{32}\.r2\.cloudflarestorage\.com', parsed.hostname or '', re.I):
            return 'cloudflare_account_host_required'
        if parsed.username is not None or parsed.password is not None:
            return 'userinfo_not_allowed'
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            return 'bucket_path_query_or_fragment_not_allowed'
        if parsed.port not in (None, 443):
            return 'unsupported_port'
        expected_netloc = parsed.hostname
        if parsed.port == 443:
            expected_netloc += ':443'
        if parsed.netloc.lower() != expected_netloc.lower():
            return 'invalid_authority'
        return None
    except ValueError:
        return 'invalid_url'


def _sanitized_error(exc: Exception) -> dict:
    code = None
    if isinstance(exc, ClientError):
        error = exc.response.get('Error', {})
        code = str(error.get('Code') or '') or None
        message = str(error.get('Message') or code or 'R2 request failed')
    else:
        message = str(exc) or 'R2 request failed'
    # Never include configured credentials, authorization material, or URL values.
    for secret in (settings.r2_access_key_id, settings.r2_secret_access_key):
        if secret:
            message = message.replace(secret, '[redacted]')
    message = re.sub(r'(?i)(authorization|x-amz-security-token|aws_secret_access_key|aws_access_key_id)(\s*[:=]\s*)\S+', r'\1\2[redacted]', message)
    message = re.sub(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', '[redacted]', message)
    message = re.sub(r'https?://\S+', '[endpoint redacted]', message)
    message = ' '.join(message.split())[:300]
    return {'error_class': type(exc).__name__, 'error_code': code,
        'error_message_sanitized': message}


def health_sync():
    enabled = settings.r2_enabled
    bucket_configured = bool(settings.r2_bucket)
    endpoint_configured = bool(settings.r2_endpoint)
    credentials_configured = bool(settings.r2_access_key_id and settings.r2_secret_access_key)
    format_issue = endpoint_format_issue(settings.r2_endpoint)
    format_valid = format_issue is None
    accessible = False
    error = None
    if enabled and bucket_configured and endpoint_configured and credentials_configured:
        if not format_valid:
            exc = ValueError(f'R2_ENDPOINT invalid format: {format_issue}.')
            error = _sanitized_error(exc)
        else:
            try:
                r2_client().head_bucket(Bucket=settings.r2_bucket)
                accessible = True
            except Exception as exc:
                error = _sanitized_error(exc)
        if error:
            log.warning('[R2] health check failed error_class=%s error_code=%s message=%s',
                error['error_class'], error['error_code'], error['error_message_sanitized'])
    return {'enabled': enabled, 'bucket_configured': bucket_configured,
        'endpoint_configured': endpoint_configured, 'credentials_configured': credentials_configured,
        'endpoint_format_valid': format_valid, 'endpoint_format_issue': format_issue,
        'accessible': accessible, **(error or {})}


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
