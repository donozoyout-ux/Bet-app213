"""Pinned immutable catalog -> bounded local read-only relational cache.

One cold load per process, no release listing or per-match GitHub fan-out.
Errors fail closed; no stale/partial catalog is advertised as complete.
"""
import asyncio
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .github import ReleaseArchive
from .history_format import MAX_CHUNK, sha, parse, unseal, validate_catalog
from .history_restore import restore
from .crypto import key_bytes


class HistoryUnavailable(Exception):
    pass


class GitHubHistoryObjects:
    """Read-only facade. Its token requires Contents:read, never write."""
    def __init__(self, repository, token, key, transport=None):
        self.remote = ReleaseArchive(repository, token, key, budget_bytes=1, keep_days=7, transport=transport)
        self.verified = False
        self.blocked_until = 0
        async def rate_limit(response):
            if response.status_code in (403, 429):
                try:
                    delay = max(float(response.headers.get('Retry-After', 60)),
                        float(response.headers.get('X-RateLimit-Reset', 0)) - time.time())
                except ValueError: delay = 60
                self.blocked_until = time.monotonic() + min(3600, max(60, delay))
        self.remote.client.event_hooks['response'] = [rate_limit]

    async def get(self, asset):
        if time.monotonic() < self.blocked_until: raise HistoryUnavailable('Archive cooldown')
        try:
            if not self.verified:
                async with self.remote.client.stream('GET', 'https://api.github.com/repos/' + self.remote.repo) as response:
                    response.raise_for_status()
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 65536: raise ValueError('Repository metadata limit')
                    metadata = parse(raw)
                if metadata.get('private') is not True or metadata.get('full_name', '').lower() != self.remote.repo.lower():
                    raise ValueError('Private archive required')
                self.verified = True
            return await self.remote.download(asset)
        except Exception:
            # Includes download rate limiting: no rapid retries or token leakage.
            self.blocked_until = max(self.blocked_until, time.monotonic() + 60)
            raise HistoryUnavailable('Archive object unavailable') from None

    async def close(self):
        await self.remote.close()


class HistoryReader:
    def __init__(self, objects, *, key, generation, catalog_asset, catalog_sha256):
        key_bytes(key)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', generation): raise ValueError('Invalid generation')
        if not re.fullmatch('[a-f0-9]{64}', catalog_sha256): raise ValueError('Invalid catalog pin')
        if type(catalog_asset['id']) is not int or catalog_asset['id'] <= 0 or not 32 <= catalog_asset['size'] <= MAX_CHUNK + 65536:
            raise ValueError('Invalid catalog asset')
        self.objects, self.key, self.generation = objects, key, generation
        self.asset, self.digest = catalog_asset, catalog_sha256
        self.lock = asyncio.Lock()
        self.engine = self.catalog = self.directory = None
        self.retry_at = 0

    async def prepare(self):
        async with self.lock:
            if self.engine is not None: return
            if time.monotonic() < self.retry_at: raise HistoryUnavailable('Archive cooldown')
            directory = tempfile.TemporaryDirectory(prefix='betapp-history-')
            try:
                async with asyncio.timeout(90):
                    blob = await self.objects.get(self.asset)
                    if len(blob) != self.asset['size'] or sha(blob) != self.digest:
                        raise ValueError('Catalog pin mismatch')
                    catalog = validate_catalog(parse(unseal(blob, self.key, self.generation, 'catalog.enc')), self.generation)
                    chunks = []
                    for meta in catalog['chunks']:
                        encrypted = await self.objects.get(meta)
                        if len(encrypted) != meta['size'] or sha(encrypted) != meta['sha256']:
                            raise ValueError('Chunk integrity mismatch')
                        chunks.append(unseal(encrypted, self.key, self.generation, meta['name']))
                    path = Path(directory.name) / 'history.sqlite'
                    # Restore never receives a production URL or session. Keep the
                    # thread alive on cancellation before removing its directory.
                    task = asyncio.create_task(asyncio.to_thread(restore, path, catalog, chunks))
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
                    engine = create_async_engine('sqlite+aiosqlite:///' + path.as_posix())
                    @event.listens_for(engine.sync_engine, 'connect')
                    def read_only(connection, record):
                        cursor = connection.cursor()
                        cursor.execute('PRAGMA query_only=ON')
                        cursor.close()
                    self.catalog, self.directory, self.engine = catalog, directory, engine
            except BaseException as exc:
                directory.cleanup()
                self.retry_at = time.monotonic() + 60
                if isinstance(exc, asyncio.CancelledError): raise
                raise HistoryUnavailable('Archive verification unavailable') from None

    @asynccontextmanager
    async def session(self):
        await self.prepare()
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            session.info['history_archive'] = dict(generation=self.generation,
                source_id=self.catalog['source_id'], coverage=self.catalog['coverage'])
            async with asyncio.timeout(12):
                yield session

    async def close(self):
        if self.engine is not None: await self.engine.dispose()
        if self.directory is not None: self.directory.cleanup()
        await self.objects.close()
        self.engine = self.directory = self.catalog = None


_reader = None


def configured_reader():
    global _reader
    if os.getenv('HISTORY_ARCHIVE_ENABLED', 'false').lower() != 'true':
        raise HistoryUnavailable('Archive disabled')
    if _reader is None:
        try:
            repo = os.environ['HISTORY_ARCHIVE_REPOSITORY']
            if repo != 'donozoyout-ux/kuponanalizii-data-archive': raise ValueError('Unapproved archive')
            key = os.environ['HISTORY_ARCHIVE_KEY']
            candidate = HistoryReader(None,
                key=key, generation=os.environ['HISTORY_ARCHIVE_GENERATION'],
                catalog_asset=dict(id=int(os.environ['HISTORY_ARCHIVE_CATALOG_ASSET_ID']),
                                   size=int(os.environ['HISTORY_ARCHIVE_CATALOG_BYTES'])),
                catalog_sha256=os.environ['HISTORY_ARCHIVE_CATALOG_SHA256'])
            candidate.objects = GitHubHistoryObjects(repo, os.environ['HISTORY_ARCHIVE_TOKEN'], key)
            _reader = candidate
        except Exception:
            raise HistoryUnavailable('Archive configuration unavailable') from None
    return _reader


async def close_reader():
    global _reader
    if _reader is not None: await _reader.close()
    _reader = None
