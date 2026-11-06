"""Cloudflare R2 object storage, using its S3-compatible API.

R2 is an archive/raw-data tier. PostgreSQL remains the operational source of truth.
Writes are best-effort by default: a temporary object-storage outage must not stop
Goaloo ingestion. Set R2_REQUIRED=true only after the bucket has been validated.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath

from src.config import settings

log = logging.getLogger('storage')


class R2Storage:
    def __init__(self):
        self.enabled = bool(settings.r2_enabled and settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key and settings.r2_bucket)
        self.required = settings.r2_required
        self._client = None
        self._lock = asyncio.Lock()

    def _build_client(self):
        if not self.enabled:
            return None
        import boto3
        return boto3.client(
            's3',
            endpoint_url=settings.r2_endpoint.rstrip('/'),
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name='auto',
        )

    async def _get_client(self):
        if not self.enabled:
            return None
        async with self._lock:
            if self._client is None:
                self._client = self._build_client()
        return self._client

    @staticmethod
    def key_for(payload: bytes, *, endpoint: str, captured_at: datetime | None = None, suffix: str = 'json') -> str:
        captured_at = captured_at or datetime.now(timezone.utc)
        captured_at = captured_at.astimezone(timezone.utc)
        digest = hashlib.sha256(payload).hexdigest()
        endpoint_digest = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()[:16]
        day = captured_at.strftime('%Y/%m/%d')
        return str(PurePosixPath('raw', 'goaloo', day, endpoint_digest, f'{digest}.{suffix}'))

    async def put_bytes(self, key: str, payload: bytes, *, content_type='application/octet-stream', metadata=None):
        client = await self._get_client()
        if client is None:
            return None
        kwargs = dict(Bucket=settings.r2_bucket, Key=key, Body=payload, ContentType=content_type)
        if metadata:
            kwargs['Metadata'] = {str(k): str(v) for k, v in metadata.items()}
        try:
            await asyncio.to_thread(client.put_object, **kwargs)
            log.info('[R2] stored key=%s bytes=%s', key, len(payload))
            return key
        except Exception:
            log.exception('[R2] put failed key=%s', key)
            if self.required:
                raise
            return None

    async def put_goaloo_response(self, *, endpoint: str, payload: bytes, captured_at: datetime | None = None, content_type='application/json'):
        key = self.key_for(payload, endpoint=endpoint, captured_at=captured_at, suffix='json')
        return await self.put_bytes(key, payload, content_type=content_type, metadata={'provider': 'goaloo', 'endpoint_hash': hashlib.sha256(endpoint.encode()).hexdigest()[:32]})


storage = R2Storage()
