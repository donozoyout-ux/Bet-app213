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
    """Create or validate one object. Verify object and manifest read-back."""
    version, league_id, created_at, payload = row
    raw = canonical_json(payload)
    digest = sha256(raw)
    uncompressed_size = len(raw)
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
        object_created = False
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
            try:
                race_digest = sha256(decompress(existing))
            except Exception:
                return {'version': version, 'status': 'HASH_CONFLICT', 'object_created': False,
                    'read_back_verified': False, 'manifest_read_back_verified': False}
            if race_digest != digest:
                return {'version': version, 'status': 'HASH_CONFLICT'}
            compressed = existing
            archived_status = 'ALREADY_ARCHIVED'
            object_created = False
        else:
            archived_status = 'ARCHIVED'
            object_created = True

    # Read back the object and verify compression, canonical JSON, size, and hash.
    try:
        readback = client.get_object(Bucket=settings.r2_bucket, Key=key)['Body'].read()
        readback_raw = decompress(readback)
        parsed_payload = json.loads(readback_raw)
        parsed_canonical = canonical_json(parsed_payload)
    except Exception:
        return {'version': version, 'status': 'READBACK_FAILED', 'object_created': object_created,
            'read_back_verified': False, 'manifest_read_back_verified': False}
    readback_digest = sha256(readback_raw)
    if (readback_digest != digest or readback_raw != raw or parsed_canonical != raw
            or len(readback_raw) != uncompressed_size or len(readback) != len(compressed)):
        return {'version': version, 'status': 'READBACK_FAILED', 'object_created': object_created,
            'read_back_verified': False, 'manifest_read_back_verified': False}

    expected_manifest = {'schema_version': 1, 'archive_type': 'board-edition', 'version': version,
        'league_id': league_id, 'created_at': created_at.isoformat() if isinstance(created_at, datetime) else str(created_at),
        'sha256': digest, 'compressed_size': len(readback), 'uncompressed_size': uncompressed_size,
        'object_key': key}
    manifest_key_exists = True
    try:
        stored_manifest_bytes = client.get_object(Bucket=settings.r2_bucket, Key=manifest_key)['Body'].read()
    except ClientError as exc:
        code = str(exc.response.get('Error', {}).get('Code', ''))
        if code not in {'NoSuchKey', '404', 'NotFound'}:
            raise
        manifest_key_exists = False

    if not manifest_key_exists:
        expected_manifest['archived_at'] = datetime.now(timezone.utc).isoformat()
        try:
            client.put_object(Bucket=settings.r2_bucket, Key=manifest_key,
                Body=canonical_json(expected_manifest), ContentType='application/json', IfNoneMatch='*')
        except ClientError as exc:
            code = str(exc.response.get('Error', {}).get('Code', ''))
            if code not in {'PreconditionFailed', '412', 'ConditionalRequestConflict'}:
                raise
        # Always fetch it again, including after a conditional-create race.
        stored_manifest_bytes = client.get_object(Bucket=settings.r2_bucket, Key=manifest_key)['Body'].read()

    try:
        stored_manifest = json.loads(stored_manifest_bytes)
    except (ValueError, UnicodeDecodeError):
        return {'version': version, 'status': 'MANIFEST_FAILED', 'object_created': object_created,
            'read_back_verified': True, 'manifest_read_back_verified': False}
    if stored_manifest.get('sha256') != digest:
        return {'version': version, 'status': 'HASH_CONFLICT', 'object_created': object_created,
            'read_back_verified': True, 'manifest_read_back_verified': False}
    manifest_ok = all(stored_manifest.get(field) == value for field, value in expected_manifest.items())
    manifest_ok = manifest_ok and isinstance(stored_manifest.get('archived_at'), str) and bool(stored_manifest['archived_at'])
    if not manifest_ok:
        return {'version': version, 'status': 'MANIFEST_FAILED', 'object_created': object_created,
            'read_back_verified': True, 'manifest_read_back_verified': False}
    return {'version': version, 'status': archived_status, 'sha256': digest,
        'object_key': key, 'manifest_key': manifest_key,
        'compressed_size': len(readback), 'uncompressed_size': uncompressed_size,
        'object_created': object_created, 'read_back_verified': True,
        'manifest_read_back_verified': True}


async def archive_rows(rows):
    results = []
    # Deliberately sequential and capped by route validation at ten records.
    for row in rows:
        try:
            result = await asyncio.to_thread(archive_one_sync, row)
        except Exception as exc:
            error = _sanitized_error(exc)
            log.error('[R2] archive failed error_class=%s error_code=%s message=%s',
                error['error_class'], error['error_code'], error['error_message_sanitized'])
            result = {'version': row[0], 'status': 'UPLOAD_FAILURE', **error,
                'read_back_verified': False, 'manifest_read_back_verified': False}
        results.append(result)
        if result.get('status') not in {'ARCHIVED', 'ALREADY_ARCHIVED'}:
            break
    return results
