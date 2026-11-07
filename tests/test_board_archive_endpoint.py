from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import src.api.main as main
import src.api.routes as routes
import src.api.storage_admin as admin
from src.storage import board_archive as archive
from src.storage import board_archive_pilot as pilot


def _published_payload(marker='one'):
    return {'items': [{'id': marker}], 'availability': {}, 'evaluated': [],
            'engine_analysis': {}, 'coupon_records': []}


async def _token(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(routes, 'settings', replace(routes.settings, scraper_token='test-admin-token'))


async def test_archive_endpoint_auth_limit_methods_and_post(api, monkeypatch):
    import src.api.storage_admin as endpoint
    await _token(monkeypatch)
    calls = []

    async def fake(_database, limit):
        calls.append(limit)
        return {'planned': limit, 'archived': limit, 'verified': limit, 'failed': 0}

    monkeypatch.setattr(endpoint, 'archive_first_board_editions', fake)
    path = '/api/admin/storage/board-editions/archive'
    assert (await api.post(path, json={'limit': 10})).status_code == 401
    headers = {'Authorization': 'Bearer test-admin-token'}
    assert (await api.post(path, json={'limit': 11}, headers=headers)).status_code == 422
    assert (await api.post(path, json={'limit': 0}, headers=headers)).status_code == 422
    assert (await api.post(path, json={}, headers=headers)).status_code == 422
    assert (await api.get(path, headers=headers)).status_code == 405
    assert (await api.delete(path, headers=headers)).status_code == 405
    response = await api.post(path, json={'limit': 10}, headers=headers)
    assert response.status_code == 200
    assert response.json()['verified'] == 10
    assert calls == [10]


async def test_endpoint_returns_controlled_unavailable_error(api, monkeypatch):
    from dataclasses import replace
    await _token(monkeypatch)

    async def fail(*_args, **_kwargs):
        raise archive.BoardArchiveError('R2 unavailable')

    monkeypatch.setattr(admin, 'archive_first_board_editions', fail)
    response = await api.post('/api/admin/storage/board-editions/archive', json={'limit': 1},
                              headers={'Authorization': 'Bearer test-admin-token'})
    assert response.status_code == 503
    assert response.json()['detail'] == 'R2 unavailable'


async def test_production_startup_does_not_start_archive(monkeypatch):
    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError('archive must never run at startup')

    monkeypatch.setattr(pilot, 'archive_first_board_editions', forbidden)
    async def close():
        return None

    monkeypatch.setattr(main, 'database', SimpleNamespace(configured=False, close=close))
    async with main.lifespan(main.app):
        pass
    assert called is False


class _Base(DeclarativeBase):
    pass


class _Edition(_Base):
    __tablename__ = 'board_editions_test'
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    league_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class _Body:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def close(self):
        pass


class _FakeS3:
    def __init__(self):
        self.objects = {}
        self.overwrites = []

    def put_object(self, **kwargs):
        key = kwargs['Key']
        old = self.objects.get(key)
        if kwargs.get('IfNoneMatch') == '*' and old is not None:
            raise _PreconditionFailed()
        if 'IfMatch' in kwargs and (old is None or old[1] != kwargs['IfMatch']):
            raise _PreconditionFailed()
        if old is not None:
            self.overwrites.append(key)
        body = kwargs['Body']
        etag = f'"etag-{len(body)}-{len(self.objects)}"'
        self.objects[key] = (body, etag)
        return {'ETag': etag}

    def get_object(self, *, Bucket, Key):
        return {'Body': _Body(self.objects[Key][0])}


class _PreconditionFailed(Exception):
    response = {'ResponseMetadata': {'HTTPStatusCode': 412}}


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        self.statements = []
        self.rolled_back = False

    async def execute(self, statement):
        self.statements.append(str(statement))

    async def scalars(self, _query):
        return _Scalars(self.rows)

    async def rollback(self):
        self.rolled_back = True


class _Database:
    configured = True
    ready = True

    def __init__(self, rows):
        self.session_obj = _Session(rows)

    @asynccontextmanager
    async def session(self):
        yield self.session_obj


def _versions(count):
    return [f'{index:064x}' for index in range(1, count + 1)]


async def test_fake_r2_pilot_verification_idempotency_checkpoint_and_hash_conflict(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(archive, 'settings', replace(archive.settings, r2_enabled=True, r2_bucket='unit-test-bucket'))
    fake = _FakeS3()
    monkeypatch.setattr(archive.r2, '_client', lambda: fake)
    created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    rows = [_Edition(version=version, league_id=7, created_at=created,
                     payload=_published_payload(version[:4])) for version in _versions(10)]
    db = _Database(rows)

    first = await pilot.archive_first_board_editions(db, 10, model=_Edition)
    assert first['planned'] == first['archived'] == first['verified'] == 10
    assert first['failed'] == 0 and first['checkpoint_saved'] is True
    assert db.session_obj.rolled_back
    assert db.session_obj.statements == ['SET TRANSACTION READ ONLY']
    checkpoint = __import__('json').loads(fake.objects[first['checkpoint_key']][0])
    assert checkpoint['complete'] is True
    assert len(checkpoint['results']) == 10
    assert len(fake.objects) == 21  # 10 immutable objects, 10 manifests, one checkpoint

    second = await pilot.archive_first_board_editions(db, 10, model=_Edition)
    assert second['already_archived'] == second['verified'] == 10
    assert second['failed'] == 0
    assert len(fake.objects) == 22  # second run adds only its own checkpoint
    assert not any(key.startswith('board-editions/v1/') and '/pilots/' not in key
                   for key in fake.overwrites)

    conflict_version = _versions(11)[-1]
    conflict_payload = _published_payload('original')
    archive.archive_board_edition(version=conflict_version, league_id=7, created_at=created,
                                  payload=conflict_payload, client=fake)
    conflict_row = _Edition(version=conflict_version, league_id=7, created_at=created,
                            payload=_published_payload('changed'))
    conflict = await pilot.archive_first_board_editions(_Database([conflict_row]), 1, model=_Edition)
    assert conflict['failed'] == 1
    assert conflict['results'][0]['error_type'] == 'HashConflict'


async def test_r2_unavailable_is_controlled_and_postgres_stays_read_only(monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(archive, 'settings', replace(archive.settings, r2_enabled=False, r2_bucket='unit-test-bucket'))
    row = _Edition(version=_versions(1)[0], league_id=7,
                   created_at=datetime.now(timezone.utc), payload=_published_payload())
    db = _Database([row])
    try:
        await pilot.archive_first_board_editions(db, 1, model=_Edition)
    except archive.BoardArchiveError as exc:
        assert str(exc) == 'R2 is disabled'
    else:
        raise AssertionError('disabled R2 should fail closed')
    assert db.session_obj.statements == []
