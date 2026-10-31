"""Disabled-by-default persistence experiment, restricted to isolated PostgreSQL.

No app Database, Base metadata, migration, route, worker or transport integration.
The SQL draft is installed by the disposable test fixture only.
"""
from datetime import date
import re
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.analytics.coupon_editions import Edition, TZ, canonical, project, propose, scope, stamp

MAX_CONTENT_BYTES = 16_384


class PublicationConflict(ValueError):
    """Equivalent to HTTP 409; transaction committed no changes."""


class StoreDisabled(RuntimeError):
    pass


def validate_test_target(url, schema):
    """Fail closed before connecting. Never accept DATABASE_URL implicitly."""
    parsed = make_url(url)
    if (parsed.drivername != 'postgresql+asyncpg' or
            parsed.host not in ('localhost', '127.0.0.1', '::1') or
            parsed.database != 'betapp_test' or parsed.query or
            not re.fullmatch(r'coupon_test_[0-9a-f]{32}', schema)):
        raise ValueError('isolated_test_postgres_required')
    return parsed


def content_text(edition):
    data = edition.as_dict()
    content = canonical({k: v for k, v in data.items() if k not in ('edition_id', 'content_digest')})
    if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise ValueError('coupon_content_byte_budget_exceeded')
    return content


def validate_proposal(edition, previous):
    """A valid hash alone is not evidence: rebuild the contract at capture time."""
    data = edition.as_dict()
    if not isinstance(data['model_version'], str) or not 1 <= len(data['model_version']) <= 100:
        raise ValueError('invalid_model_version_length')
    rebuilt = propose(dict(kind=data['kind'], items=data['items'], reason=data['reason'],
        status='candidate_only' if data['items'] else 'unavailable'), day=data['day'],
        now=stamp(data['captured_as_of']), model_version=data['model_version'],
        previous=previous, new_revision=previous is not None)
    if rebuilt.document != edition.document:
        raise ValueError('invalid_publication_contract_or_lineage')
    content_text(edition)


class IsolatedEditionStore:
    """Explicit test-only feature flag; no production enablement path.

    Each append owns its transaction. A unique revision insert plus head CAS
    is atomic. Concurrent different edits conflict; identical retries return
    the already committed immutable edition without moving the head backwards.
    """
    def __init__(self, url, schema, *, enabled=False):
        if enabled is not True:
            raise StoreDisabled('coupon_store_disabled')
        validate_test_target(url, schema)
        self.schema = schema
        self.engine = create_async_engine(url, connect_args={'timeout': 5, 'command_timeout': 15})

    async def close(self):
        await self.engine.dispose()

    def _table(self, name):
        return f'"{self.schema}".{name}'

    async def _read(self, conn, edition_id):
        row = (await conn.execute(text(f'SELECT content, content_digest, edition_id FROM {self._table("coupon_edition")} WHERE edition_id=:id'),
                                  {'id': edition_id})).mappings().one_or_none()
        if row is None:
            return None
        import json
        return Edition(canonical(dict(json.loads(row['content']),
            edition_id=row['edition_id'], content_digest=row['content_digest'])))

    async def read(self, edition_id):
        async with self.engine.connect() as conn:
            return await self._read(conn, edition_id)

    async def head(self, day, kind):
        scope(day, kind)
        async with self.engine.connect() as conn:
            row = (await conn.execute(text(f'SELECT e.content, e.content_digest, e.edition_id FROM {self._table("coupon_head")} h JOIN {self._table("coupon_edition")} e ON e.day=h.day AND e.kind=h.kind AND e.edition_id=h.edition_id WHERE h.day=:day AND h.kind=:kind'),
                dict(day=date.fromisoformat(day), kind=kind))).mappings().one_or_none()
            if row is None:
                return None
            import json
            return Edition(canonical(dict(json.loads(row['content']),
                edition_id=row['edition_id'], content_digest=row['content_digest'])))

    async def append(self, edition, *, expected_digest, now):
        # Roundtrip even if callers provide an object forged outside the dataclass.
        edition = Edition(edition.document)
        data = edition.as_dict()
        content = content_text(edition)
        if expected_digest is not None and not re.fullmatch(r'[0-9a-f]{64}', expected_digest):
            raise ValueError('invalid_expected_digest')
        expected_id = 'ce1:' + expected_digest if expected_digest is not None else None
        if data['previous_id'] != expected_id:
            raise PublicationConflict('expected_previous_digest_mismatch')
        now = stamp(now)
        try:
            async with self.engine.begin() as conn:
                existing = await self._read(conn, data['edition_id'])
                if existing:
                    if existing.document != edition.document:
                        raise ValueError('stored_digest_collision')
                    return existing
                previous = await self._read(conn, expected_id) if expected_id else None
                if expected_id and previous is None:
                    raise PublicationConflict('previous_edition_missing')
                validate_proposal(edition, previous)
                if now.astimezone(TZ).date().isoformat() != data['day']:
                    raise ValueError('outside_publication_day')
                if project(edition, now)['status'] == 'stale':
                    raise ValueError('stale_new_publication')
                params = dict(storage_id=uuid4(), day=date.fromisoformat(data['day']), kind=data['kind'],
                    revision=data['revision'], content_digest=data['content_digest'], edition_id=data['edition_id'],
                    previous_id=data['previous_id'], first_id=data['first_edition_id'] or data['edition_id'],
                    captured_as_of=stamp(data['captured_as_of']), model_version=data['model_version'], content=content)
                await conn.execute(text(f'''INSERT INTO {self._table('coupon_edition')}
                    (storage_id,day,kind,revision,content_digest,edition_id,previous_id,first_id,captured_as_of,model_version,content)
                    VALUES (:storage_id,:day,:kind,:revision,:content_digest,:edition_id,:previous_id,:first_id,:captured_as_of,:model_version,:content)'''), params)
                if expected_id is None:
                    changed = await conn.scalar(text(f'''INSERT INTO {self._table('coupon_head')} (day,kind,edition_id)
                        VALUES (:day,:kind,:edition_id) ON CONFLICT (day,kind) DO NOTHING RETURNING edition_id'''), params)
                else:
                    changed = await conn.scalar(text(f'''UPDATE {self._table('coupon_head')} SET edition_id=:edition_id
                        WHERE day=:day AND kind=:kind AND edition_id=:expected RETURNING edition_id'''), dict(params, expected=expected_id))
                if changed is None:
                    raise PublicationConflict('head_compare_and_swap_failed')
                return edition
        except IntegrityError as exc:
            # A concurrent identical insert may have committed while we waited on
            # its unique index. Use a fresh transaction after the failed one rolls back.
            existing = await self.read(data['edition_id'])
            if existing and existing.document == edition.document:
                return existing
            raise PublicationConflict('concurrent_revision_or_integrity_conflict') from exc
