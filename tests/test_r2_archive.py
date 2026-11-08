import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.archive import r2
from src.archive.github_oidc import REPOSITORY, REF, SUBJECT, WORKFLOW, allowed_pilot_claims
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
        r2_endpoint='https://' + ('a' * 32) + '.r2.cloudflarestorage.com', r2_access_key_id='test-key',
        r2_secret_access_key='test-secret', scraper_token='')
    monkeypatch.setattr(r2.boto3, 'client', lambda service, **kwargs: client)
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
    assert result['manifest_read_back_verified'] is True
    assert result['compressed_size'] == len(stored := fake_r2.objects[('unit-test-bucket', result['object_key'])])
    assert result['uncompressed_size'] == len(r2.canonical_json(sample_row()[3]))
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
                    'credentials_configured': True, 'endpoint_format_valid': True,
                    'endpoint_format_issue': None, 'accessible': True}
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
    assert response.json()['records_selected'] == 0


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
    assert response.json()['postgres_source_preserved'] is True
    assert response.json()['results'][0]['postgres_source_preserved'] is True
    assert response.json()['postgres_writes'] == 0
    assert response.json()['postgres_deletes'] == 0


@pytest.mark.asyncio
async def test_archive_batch_stops_at_first_hash_conflict(fake_r2):
    first = sample_row({'other': 'value'})
    second = ('ver-b2c3', 36, first[2], {'should_not': 'be processed'})
    first_key = ('unit-test-bucket', f'board-editions/v1/{first[0]}.json.zst')
    fake_r2.objects[first_key] = r2.compress(r2.canonical_json({'already': 'different'}))
    results = await r2.archive_rows([first, second])
    assert len(results) == 1
    assert results[0]['status'] == 'HASH_CONFLICT'
    assert ('unit-test-bucket', f'board-editions/v1/{second[0]}.json.zst') not in fake_r2.objects


def test_manifest_readback_conflict_is_not_overwritten(fake_r2):
    result = r2.archive_one_sync(sample_row())
    manifest_key = ('unit-test-bucket', result['manifest_key'])
    original = fake_r2.objects[manifest_key]
    manifest = json.loads(original)
    manifest['sha256'] = '0' * 64
    fake_r2.objects[manifest_key] = r2.canonical_json(manifest)
    second = r2.archive_one_sync(sample_row())
    assert second['status'] == 'HASH_CONFLICT'
    assert fake_r2.objects[manifest_key] != original


def test_github_oidc_pilot_claims_are_pinned_to_manual_master_workflow():
    claims = {'repository': REPOSITORY, 'ref': REF, 'workflow_ref': WORKFLOW,
        'event_name': 'workflow_dispatch', 'sub': SUBJECT}
    assert allowed_pilot_claims(claims)
    for key, bad_value in [('repository', 'attacker/fork'), ('ref', 'refs/heads/feature'),
                           ('workflow_ref', 'attacker workflow'), ('event_name', 'push'),
                           ('sub', 'wrong-subject')]:
        assert not allowed_pilot_claims({**claims, key: bad_value})


def test_oidc_pilot_trigger_is_hard_capped_at_ten_and_read_only(monkeypatch, fake_r2):
    rows = [(f'ver-{index:02}', 36, sample_row()[2], {'ordinal': index}) for index in range(10)]

    class Result:
        def all(self): return rows
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, statement):
            sql = str(statement).lower()
            assert statement.is_select
            assert 'delete' not in sql and 'update' not in sql
            if statement._limit_clause is not None:
                assert statement._limit_clause.value == 10
                assert 'order by board_editions.version' in sql
            return Result()
    class DB:
        configured = ready = True
        def session(self): return Session()

    claims = {'repository': REPOSITORY, 'ref': REF, 'workflow_ref': WORKFLOW,
        'event_name': 'workflow_dispatch', 'sub': SUBJECT}
    monkeypatch.setattr(main, 'database', DB())
    monkeypatch.setattr(main, 'verify_github_actions_token', lambda token: claims)
    response = TestClient(main.app).post('/api/admin/storage/board-editions/archive-pilot',
        json={'limit': 999}, headers={'Authorization': 'Bearer dummy-oidc-token'})
    assert response.status_code == 200
    body = response.json()
    assert body['records_selected'] == 10
    assert body['records_archived'] == 10
    assert body['readback_pass'] == 10
    assert body['manifest_pass'] == 10
    assert body['postgres_source_preserved'] is True
    assert body['postgres_writes'] == body['postgres_deletes'] == 0
    assert body['r2_objects_created'] == 10
    assert len(body['results']) == 10


def test_oidc_pilot_trigger_rejects_missing_or_untrusted_identity_before_database(monkeypatch, fake_r2):
    class DB:
        configured = ready = True
        def session(self): raise AssertionError('Database must not be accessed before OIDC authorization.')

    monkeypatch.setattr(main, 'database', DB())
    client = TestClient(main.app)
    missing = client.post('/api/admin/storage/board-editions/archive-pilot')
    assert missing.status_code == 401

    claims = {'repository': 'attacker/fork', 'ref': REF, 'workflow_ref': WORKFLOW,
        'event_name': 'workflow_dispatch', 'sub': SUBJECT}
    monkeypatch.setattr(main, 'verify_github_actions_token', lambda token: claims)
    rejected = client.post('/api/admin/storage/board-editions/archive-pilot',
        headers={'Authorization': 'Bearer dummy-oidc-token'})
    assert rejected.status_code == 403
    assert 'dummy-oidc-token' not in rejected.text


def test_endpoint_format_rejects_bucket_or_path():
    base = 'https://' + ('a' * 32) + '.r2.cloudflarestorage.com'
    assert r2.endpoint_format_valid(base)
    assert r2.endpoint_format_valid(base + '/')
    assert not r2.endpoint_format_valid(base + '/unit-test-bucket')
    assert not r2.endpoint_format_valid('http://' + ('a' * 32) + '.r2.cloudflarestorage.com')
    assert r2.endpoint_format_issue(base + '\n') == 'control_whitespace'
    assert r2.endpoint_format_issue(' ' + base) == 'control_whitespace'


def test_health_exposes_sanitized_client_error_without_secrets(monkeypatch, fake_r2):
    from botocore.exceptions import ClientError

    class BrokenClient:
        def head_bucket(self, **kwargs):
            raise ClientError({'Error': {'Code': 'SignatureDoesNotMatch',
                'Message': 'bad signature test-secret https://account.example/path'}}, 'HeadBucket')

    monkeypatch.setattr(r2.boto3, 'client', lambda service, **kwargs: BrokenClient())
    result = r2.health_sync()
    assert result['accessible'] is False
    assert result['error_class'] == 'ClientError'
    assert result['error_code'] == 'SignatureDoesNotMatch'
    assert 'test-secret' not in result['error_message_sanitized']
    assert 'https://account.example/path' not in result['error_message_sanitized']


def test_r2_client_uses_endpoint_url_and_secret_env_config(monkeypatch, fake_r2):
    seen = {}
    monkeypatch.setattr(r2.boto3, 'client', lambda service, **kwargs: seen.update(service=service, **kwargs) or object())
    r2.r2_client()
    assert seen['service'] == 's3'
    assert seen['endpoint_url'] == r2.settings.r2_endpoint
    assert seen['aws_access_key_id'] == 'test-key'
    assert seen['aws_secret_access_key'] == 'test-secret'
