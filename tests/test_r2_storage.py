from datetime import datetime, timezone

from src.storage.r2 import R2Storage


def test_r2_key_is_content_addressed_and_partitioned():
    captured = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
    payload = b'goaloo-payload'
    key1 = R2Storage.key_for(payload, endpoint='https://www.goaloo.com/api/test', captured_at=captured)
    key2 = R2Storage.key_for(payload, endpoint='https://www.goaloo.com/api/test', captured_at=captured)
    assert key1 == key2
    assert key1.startswith('raw/goaloo/2026/11/06/')
    assert key1.endswith('.json')


def test_r2_disabled_without_configuration(monkeypatch):
    from src import config
    monkeypatch.setattr(config.settings, 'r2_enabled', False)
    storage = R2Storage()
    assert storage.enabled is False
