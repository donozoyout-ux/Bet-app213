from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
import zstandard

from src.storage import board_archive as archive


VERSION = 'a' * 64
CREATED = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
PAYLOAD = {
    'items': [{'match_id': 8}],
    'availability': [],
    'evaluated': 1,
    'engine_analysis': [],
    'coupon_records': [],
}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_count = 0

    def put_object(self, **kwargs):
        key = kwargs['Key']
        if key in self.objects:
            exc = RuntimeError('precondition failed')
            exc.response = {'ResponseMetadata': {'HTTPStatusCode': 412}}
            raise exc
        self.objects[key] = kwargs['Body']
        self.put_count += 1

    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(self.objects[kwargs['Key']])}


def setup(monkeypatch, *, enabled=True):
    client = FakeS3()
    monkeypatch.setattr(archive, 'settings', SimpleNamespace(r2_enabled=enabled, r2_bucket='test-bucket'))
    monkeypatch.setattr(archive.r2, '_client', lambda: client)
    return client


def test_canonical_bytes_and_hash_ignore_mapping_order():
    one = {'a': 1, 'b': {'ş': [2, 3]}}
    two = {'b': {'ş': [2, 3]}, 'a': 1}
    assert archive.canonical_bytes(one) == archive.canonical_bytes(two)
    assert archive.payload_sha256(one) == archive.payload_sha256(two)


def test_archive_read_and_verify_pass(monkeypatch):
    client = setup(monkeypatch)
    result = archive.archive_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    assert result['status'] == 'ARCHIVED'
    assert archive.read_archived_board_edition(VERSION, client=client) == PAYLOAD
    verified = archive.verify_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    assert verified['sha256'] == archive.payload_sha256(PAYLOAD)


def test_archive_repeat_is_idempotent(monkeypatch):
    client = setup(monkeypatch)
    first = archive.archive_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    puts_after_first = client.put_count
    second = archive.archive_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    assert first['status'] == 'ARCHIVED'
    assert second['status'] == 'ALREADY_ARCHIVED'
    assert client.put_count == puts_after_first
    assert len(client.objects) == 2


def test_same_version_with_different_payload_is_hash_conflict(monkeypatch):
    client = setup(monkeypatch)
    archive.archive_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    changed = {**PAYLOAD, 'evaluated': 2}
    with pytest.raises(archive.HashConflict, match='different payload SHA-256'):
        archive.archive_board_edition(version=VERSION, league_id=23,
            created_at=CREATED, payload=changed, client=client)


def test_corrupted_compressed_object_fails_verification(monkeypatch):
    client = setup(monkeypatch)
    client.objects[archive.object_key(VERSION)] = b'not-zstandard-data'
    with pytest.raises(archive.VerificationError, match='corrupted'):
        archive.archive_board_edition(version=VERSION, league_id=23,
            created_at=CREATED, payload=PAYLOAD, client=client)


def test_invalid_json_archive_fails_read(monkeypatch):
    client = setup(monkeypatch)
    raw = b'{invalid json'
    compressed = zstandard.ZstdCompressor().compress(raw)
    key = archive.object_key(VERSION)
    manifest = {
        'schema_version': 1, 'archive_type': 'board_edition', 'version': VERSION,
        'league_id': 23, 'created_at': CREATED.isoformat().replace('+00:00', 'Z'),
        'sha256': hashlib.sha256(raw).hexdigest(), 'compressed_size': len(compressed),
        'uncompressed_size': len(raw), 'object_key': key,
        'archived_at': '2026-11-01T00:00:00Z',
    }
    client.objects[key] = compressed
    client.objects[archive.manifest_key(VERSION)] = json.dumps(manifest).encode()
    with pytest.raises(archive.VerificationError, match='invalid'):
        archive.read_archived_board_edition(VERSION, client=client)


def test_manifest_contains_required_metadata(monkeypatch):
    client = setup(monkeypatch)
    result = archive.archive_board_edition(version=VERSION, league_id=23,
        created_at=CREATED, payload=PAYLOAD, client=client)
    manifest = json.loads(client.objects[result['manifest_key']])
    assert manifest == result['manifest']
    assert set(manifest) == {'schema_version', 'archive_type', 'version', 'league_id',
        'created_at', 'sha256', 'compressed_size', 'uncompressed_size', 'object_key', 'archived_at'}
    assert manifest['sha256'] == archive.payload_sha256(PAYLOAD)
    assert manifest['object_key'] == f'{archive.PREFIX}/{VERSION}.json.zst'


def test_r2_disabled_is_controlled_failure(monkeypatch):
    setup(monkeypatch, enabled=False)
    with pytest.raises(archive.BoardArchiveError, match='R2 is disabled'):
        archive.archive_board_edition(version=VERSION, league_id=23,
            created_at=CREATED, payload=PAYLOAD)


def test_r2_required_false_does_not_mask_explicit_archive_failure(monkeypatch):
    client = setup(monkeypatch)
    # R2_REQUIRED=false controls best-effort scraper capture. An explicit archive
    # command must still report a failed write instead of claiming success.
    monkeypatch.setattr(archive, 'settings', SimpleNamespace(r2_enabled=False,
        r2_required=False, r2_bucket='test-bucket'))
    with pytest.raises(archive.BoardArchiveError, match='R2 is disabled'):
        archive.archive_board_edition(version=VERSION, league_id=23,
            created_at=CREATED, payload=PAYLOAD, client=client)


@pytest.mark.asyncio
async def test_cli_dry_run_reports_plan_without_r2_write(monkeypatch):
    from tools import archive_board_edition as cli

    async def fake_load(_version):
        return VERSION, 23, CREATED, PAYLOAD

    monkeypatch.setattr(cli, 'load_edition', fake_load)
    monkeypatch.setattr(cli, 'archive_board_edition', lambda **_kwargs: pytest.fail('dry-run wrote to R2'))
    result = await cli.run(VERSION, dry_run=True)
    assert result['status'] == 'DRY_RUN'
    assert result['sha256'] == archive.payload_sha256(PAYLOAD)
    assert result['object_key'] == archive.object_key(VERSION)
