import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.archive import r2
from src.api import main


class FakeR2:
    def __init__(self):
        self.objects = {}

    def head_bucket(self, **kwargs):
        return {}

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        return {'Body': io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, **kwargs):
        if IfNoneMatch == '*' and (Bucket, Key) in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.objects[(Bucket, Key)] = Body
        return {}


@pytest.fixture
def fake_r2(monkeypatch):
    client = FakeR2()
    cfg = SimpleNamespace(r2_enabled=True, r2_bucket='unit-test-bucket',
        r2_endpoint='https://example.invalid', r2_access_key_id='test-key',
        r2_secret_access_key='test-secret', scraper_token='')
    monkeypatch.setattr(r2, 'r2_client', lambda: client)
    monkeypatch.setattr(r2, 'settings', cfg)
    monkeypatch.setattr(main, 'settings', cfg)
    return client


def sample_row(payload=None):
    return ('ver-a1b2', 36, datetime(2026, 1, 2, tzinfo=timezone.utc),
            payload if payload is not None else {'z': [1, 2], 'a': 'ş'} )


def test_canonical_json_zstd_and_sha_parity():
    left = r2.canonical_json({'b': 2, 'a': 1})
    right = r2.canonical_json({'a': 1, 'b': 2})
    assert left == right
    assert r2.decompress(r2.compress(left)) == left
    assert r2.sha256(left) == r2.sha256(right)


def test_upload_readback_and_manifest(fake_r2):
    result = r2.archive_one_sync(sample_row())
    assert result['status'] == 'ARCHIVED'
    assert result['read_back_verified'] is True
    assert result['postgres_payload_unchanged'] is True
    stored = fake_r2.objects[('unit-test-bucket', result['object_key'])]
    assert r2.decompress(stored) == r2.canonical_json(sample_row()[3])
    manifest = json.loads(fake_r2.objects[('unit-test-bucket', result['manifest_key'])])
    assert manifest['sha256'] == r2.sha256(r2.canonical_json(sample_row()[3]))
    assert manifest['version'] == 'ver-a1b2'


def test_duplicate_same_hash_is_idempotent(fake_r2):
    first = r2.archive_one_sync(sample_row())
    second = r2.archive_one_sync(sample_row())
    assert first['status'] == 'ARCHIVED'
    assert second['status'] == 'ALREADY_ARCHIVED'


def test_duplicate_different_hash_conflicts_without_overwrite(fake_r2):
    first = r2.archive_one_sync(sample_row())
    original = fake_r2.objects[('unit-test-bucket', first['object_key'])]
    result = r2.archive_one_sync(sample_row({'changed': True}))
    assert result['status'] == 'HASH_CONFLICT'
    assert fake_r2.objects[('unit-test-bucket', first['object_key'])] == original


def test_health_endpoint_reports_configuration_without_secrets(monkeypatch, fake_r2):
    response = TestClient(main.app).get('/api/storage/health')
    assert response.status_code == 200
    body = response.json()
    assert body == {'enabled': True, 'bucket_configured': True, 'endpoint_configured': True,
                    'credentials_configured': True, 'accessible': True}
    assert 'test-secret' not in response.text


@pytest.mark.parametrize('limit', [1, 10])
def test_archive_endpoint_accepts_limit_bounds(monkeypatch, fake_r2, limit):
    class Result:
        def all(self):
            return []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, statement):
            assert statement.is_select
            return Result()
    class DB:
        configured = ready = True
        def session(self): return Session()
    monkeypatch.setattr(main, 'database', DB())
    main.settings.scraper_token = 'admin-test-token'
    client = TestClient(main.app)
    response = client.post('/api/admin/storage/board-editions/archive', json={'limit': limit},
                           headers={'Authorization': 'Bearer admin-test-token'})
    assert response.status_code == 200
    assert response.json()['read'] == 0


@pytest.mark.parametrize('payload,expected', [({}, 422), ({'limit': 0}, 422), ({'limit': 11}, 422)])
def test_archive_endpoint_rejects_missing_or_out_of_range_limit(fake_r2, payload, expected):
    response = TestClient(main.app).post('/api/admin/storage/board-editions/archive', json=payload,
                                         headers={'Authorization': 'Bearer admin-test-token'})
    assert response.status_code == expected


def test_archive_endpoint_requires_bearer_auth(fake_r2, monkeypatch):
    main.settings.scraper_token = 'admin-test-token'
    response = TestClient(main.app).post('/api/admin/storage/board-editions/archive', json={'limit': 1})
    assert response.status_code == 401


def test_archive_query_is_read_only(monkeypatch, fake_r2):
    class Result:
        def all(self): return [sample_row()]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, statement):
            sql = str(statement).lower()
            assert statement.is_select
            assert 'delete' not in sql and 'update' not in sql
            assert 'board_editions' in sql
            return Result()
    class DB:
        configured = ready = True
        def session(self): return Session()
    monkeypatch.setattr(main, 'database', DB())
    main.settings.scraper_token = 'admin-test-token'
    response = TestClient(main.app).post('/api/admin/storage/board-editions/archive', json={'limit': 1},
        headers={'Authorization': 'Bearer admin-test-token'})
    assert response.status_code == 200
    assert response.json()['results'][0]['postgres_payload_unchanged'] is True
