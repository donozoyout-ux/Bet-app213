import asyncio
from datetime import datetime, timezone
import hashlib
import io
import logging
from types import SimpleNamespace

import pytest

from src.storage import r2


class FakeS3:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail
        self.puts = 0

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError('credential=NEVER-LOG-THIS secret=NEVER-LOG-THIS')
        self.puts += 1
        key = kwargs['Key']
        if key in self.objects:
            error = RuntimeError('precondition')
            error.response = {'ResponseMetadata': {'HTTPStatusCode': 412}}
            raise error
        self.objects[key] = kwargs['Body']

    def get_object(self, **kwargs):
        if self.fail:
            raise RuntimeError('unavailable')
        return {'Body': io.BytesIO(self.objects[kwargs['Key']])}

    def delete_object(self, **kwargs):
        if self.fail:
            raise RuntimeError('unavailable')
        self.objects.pop(kwargs['Key'], None)


def configure(monkeypatch, *, enabled=True, required=False, raw=True, client=None):
    config = SimpleNamespace(r2_enabled=enabled, r2_required=required,
        r2_raw_capture_enabled=raw, r2_endpoint='https://acct.r2.cloudflarestorage.com',
        r2_bucket='test-bucket', r2_access_key_id='ACCESS-SECRET', r2_secret_access_key='SECRET-SECRET')
    monkeypatch.setattr(r2, 'settings', config)
    if client is not None:
        monkeypatch.setattr(r2, '_client', lambda: client)


def test_r2_disabled_mode(monkeypatch):
    configure(monkeypatch, enabled=False)
    assert r2.health_check() == {'r2': 'disabled', 'bucket': 'test-bucket',
        'write_test': False, 'read_test': False, 'delete_test': False}


def test_key_generation():
    date = datetime(2026, 11, 6, tzinfo=timezone.utc)
    payload = {'b': 2, 'a': [1, 'ş']}
    key = r2.raw_object_key('https://www.goaloo.com/ajax', payload, now=date, params={'id': 9})
    assert key == r2.raw_object_key('https://www.goaloo.com/ajax', {'a': [1, 'ş'], 'b': 2}, now=date, params={'id': 9})
    assert key.startswith('raw/goaloo/2026/11/06/') and key.endswith('.json')


def test_content_hash_is_deterministic():
    payload = {'b': 2, 'a': [1, 'ş']}
    digest = hashlib.sha256(r2.canonical_payload(payload)).hexdigest()
    assert digest == hashlib.sha256(r2.canonical_payload({'a': [1, 'ş'], 'b': 2})).hexdigest()


def test_r2_write_read_delete_and_hash(monkeypatch):
    fake = FakeS3()
    configure(monkeypatch, client=fake)
    result = r2.health_check()
    assert result == {'r2': 'ok', 'bucket': 'test-bucket', 'write_test': True,
                      'read_test': True, 'delete_test': True}
    assert fake.objects == {}


@pytest.mark.asyncio
async def test_goaloo_raw_capture_deduplicates(monkeypatch):
    from src.scrapers.goaloo import client as goaloo
    fake = FakeS3()
    configure(monkeypatch, client=fake)
    monkeypatch.setattr(goaloo, 'settings', SimpleNamespace(request_timeout=1, concurrency=1,
        retries=1, request_interval=0, r2_enabled=True, r2_raw_capture_enabled=True, r2_required=False))
    class Response:
        url = 'https://www.goaloo.com/ajax?id=1'
        text = '{"b":2,"a":1}'
        def raise_for_status(self): pass
    class Http:
        async def get(self, *args, **kwargs): return Response()
        async def aclose(self): pass
    c = goaloo.GoalooClient()
    await c.client.aclose()
    c.client = Http()
    assert await c.get('https://www.goaloo.com/ajax', {'id': 1}) == {'b': 2, 'a': 1}
    assert await c.get('https://www.goaloo.com/ajax', {'id': 1}) == {'b': 2, 'a': 1}
    assert len(fake.objects) == 1
    # The second conditional request is rejected as already present; only one
    # object body is retained, so retries cannot create another physical copy.
    assert len(fake.objects) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('required,raises', [(False, False), (True, True)])
async def test_r2_unavailable_required_policy(monkeypatch, required, raises):
    from src.scrapers.goaloo import client as goaloo
    configure(monkeypatch, required=required, client=FakeS3(fail=True))
    monkeypatch.setattr(goaloo, 'settings', SimpleNamespace(request_timeout=1, concurrency=1,
        retries=1, request_interval=0, r2_enabled=True, r2_raw_capture_enabled=True, r2_required=required))
    class Response:
        url = 'https://www.goaloo.com/ajax'
        text = '{"ok":true}'
        def raise_for_status(self): pass
    class Http:
        async def get(self, *args, **kwargs): return Response()
        async def aclose(self): pass
    c = goaloo.GoalooClient()
    await c.client.aclose()
    c.client = Http()
    if raises:
        with pytest.raises(r2.StorageError):
            await c.get('https://www.goaloo.com/ajax')
    else:
        assert await c.get('https://www.goaloo.com/ajax') == {'ok': True}


def test_secret_values_never_appear_in_logs(monkeypatch, caplog):
    fake = FakeS3(fail=True)
    configure(monkeypatch, client=fake)
    with caplog.at_level(logging.WARNING):
        result = r2.health_check()
    assert result['r2'] == 'error'
    assert 'ACCESS-SECRET' not in caplog.text
    assert 'SECRET-SECRET' not in caplog.text
    assert 'NEVER-LOG-THIS' not in caplog.text
