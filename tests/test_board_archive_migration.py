from datetime import datetime, timezone
import io
from types import SimpleNamespace

import pytest

from src.storage import board_archive as archive
from tools import archive_board_editions as migration


CREATED = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
PAYLOAD = {'items': [], 'availability': [], 'evaluated': 0,
           'engine_analysis': [], 'coupon_records': []}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_count = 0

    def put_object(self, **kwargs):
        key = kwargs['Key']
        if key in self.objects:
            exc = RuntimeError('already exists')
            exc.response = {'ResponseMetadata': {'HTTPStatusCode': 412}}
            raise exc
        self.objects[key] = kwargs['Body']
        self.put_count += 1

    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(self.objects[kwargs['Key']])}


def entries(count=3):
    return [{'version': f'{index:064x}', 'league_id': 8,
             'created_at': migration._timestamp(CREATED)} for index in range(1, count + 1)]


def fetcher_for(plan):
    by_version = {entry['version']: {
        'version': entry['version'], 'league_id': entry['league_id'],
        'created_at': CREATED, 'payload': PAYLOAD,
    } for entry in plan}
    calls = []

    async def fetch(versions):
        calls.append(list(versions))
        return {version: by_version[version] for version in versions}
    return fetch, calls


@pytest.mark.asyncio
async def test_inventory_capture_uses_readonly_selects():
    class ReadOnlyTransaction:
        def __init__(self, outer, readonly):
            self.outer, self.readonly = outer, readonly
        async def __aenter__(self):
            self.outer.readonly_flags.append(self.readonly)
        async def __aexit__(self, *_args):
            return False

    class FakeConnection:
        def __init__(self):
            self.readonly_flags = []
            self.sql = []
        def transaction(self, *, readonly=False):
            return ReadOnlyTransaction(self, readonly)
        async def fetchval(self, sql):
            self.sql.append(sql)
            return 'production_db'
        async def fetch(self, sql):
            self.sql.append(sql)
            return [{'version': row['version'], 'league_id': row['league_id'],
                     'created_at': CREATED} for row in entries()]

    connection = FakeConnection()
    database, plan = await migration.snapshot_edition_list(connection)
    assert database == 'production_db'
    assert len(plan) == 3
    assert connection.readonly_flags == [True]
    assert all(sql.lstrip().upper().startswith('SELECT') for sql in connection.sql)


@pytest.mark.asyncio
async def test_full_dry_run_is_bounded_and_performs_no_r2_writes():
    plan = entries()
    fetch, calls = fetcher_for(plan)
    summary = await migration.process_plan(plan=plan, fetch_batch=fetch,
        mode='dry-run', batch_size=2)
    assert summary['selected'] == summary['processed'] == 3
    assert summary['complete'] is True
    assert summary['uncompressed_bytes'] > 0
    assert summary['compressed_bytes'] > 0
    assert [len(call) for call in calls] == [2, 1]


@pytest.mark.asyncio
async def test_archive_pass_is_idempotent_and_verifiable(monkeypatch):
    monkeypatch.setattr(archive, 'settings', SimpleNamespace(r2_enabled=True, r2_bucket='test-bucket'))
    client = FakeS3()
    plan = entries()
    fetch, _ = fetcher_for(plan)
    first = await migration.process_plan(plan=plan, fetch_batch=fetch,
        mode='execute', client=client, batch_size=2)
    puts_after_first_pass = client.put_count
    second = await migration.process_plan(plan=plan, fetch_batch=fetch,
        mode='execute', client=client, batch_size=2)
    assert first['archived'] == 3
    assert first['already_archived'] == 0
    assert second['archived'] == 0
    assert second['already_archived'] == 3
    assert client.put_count == puts_after_first_pass
    assert len(client.objects) == 6  # one immutable object and one manifest per edition


@pytest.mark.asyncio
async def test_batch_limit_and_checkpoint_cursor():
    plan = entries(5)
    fetch, calls = fetcher_for(plan)
    progress = []
    summary = await migration.process_plan(plan=plan, fetch_batch=fetch,
        mode='dry-run', start_at=1, limit=2, batch_size=1,
        checkpoint=lambda index, _summary, version: progress.append((index, version)))
    assert summary['processed'] == 2
    assert summary['complete'] is False
    assert len(calls) == 2
    assert progress == [(2, plan[1]['version']), (3, plan[2]['version'])]


@pytest.mark.asyncio
async def test_metadata_mismatch_aborts_before_any_r2_write(monkeypatch):
    monkeypatch.setattr(archive, 'settings', SimpleNamespace(r2_enabled=True, r2_bucket='test-bucket'))
    plan = entries(1)
    client = FakeS3()

    async def changed_row(_versions):
        return {plan[0]['version']: {
            'version': plan[0]['version'], 'league_id': 9,
            'created_at': CREATED, 'payload': PAYLOAD,
        }}

    with pytest.raises(RuntimeError, match='metadata changed'):
        await migration.process_plan(plan=plan, fetch_batch=changed_row,
            mode='execute', client=client)
    assert client.put_count == 0


def test_checkpoint_json_is_atomically_written(tmp_path):
    path = tmp_path / 'checkpoint.json'
    migration._write_checkpoint(path, {'next_index': 12, 'complete': False})
    assert migration._read_json(path) == {'next_index': 12, 'complete': False}
    assert not path.with_name(path.name + '.tmp').exists()


def test_plan_digest_is_stable_and_detects_inventory_changes():
    plan = entries()
    assert migration._plan_sha256(plan) == migration._plan_sha256(list(plan))
    changed = [*plan, {**plan[-1], 'version': 'f' * 64}]
    assert migration._plan_sha256(plan) != migration._plan_sha256(changed)


@pytest.mark.asyncio
async def test_execute_checkpoint_resumes_without_skipping_or_rewriting(tmp_path, monkeypatch):
    plan = entries()
    source_rows = {item['version']: {
        'version': item['version'], 'league_id': item['league_id'],
        'created_at': CREATED, 'payload': PAYLOAD,
    } for item in plan}

    class ReadOnlyTransaction:
        def __init__(self, outer, readonly):
            self.outer, self.readonly = outer, readonly
        async def __aenter__(self):
            self.outer.readonly_flags.append(self.readonly)
        async def __aexit__(self, *_args):
            return False

    class FakeConnection:
        def __init__(self):
            self.readonly_flags = []
            self.sql = []
        def transaction(self, *, readonly=False):
            return ReadOnlyTransaction(self, readonly)
        async def fetchval(self, sql):
            self.sql.append(sql)
            return 'production_db'
        async def fetch(self, sql, *args):
            self.sql.append(sql)
            if 'ORDER BY created_at, version' in sql:
                return [{'version': item['version'], 'league_id': item['league_id'],
                         'created_at': CREATED} for item in plan]
            versions = args[0]
            return [source_rows[version] for version in versions]
        async def close(self):
            return None

    connections = []
    async def connect():
        conn = FakeConnection()
        connections.append(conn)
        return conn

    client = FakeS3()
    monkeypatch.setattr(migration, 'connect_database', connect)
    monkeypatch.setattr(migration, 'settings', SimpleNamespace(database_url='configured',
        r2_bucket='test-bucket'))
    monkeypatch.setattr(archive, 'settings', SimpleNamespace(r2_enabled=True,
        r2_bucket='test-bucket'))
    monkeypatch.setattr(archive, '_client', lambda supplied=None: supplied or client)
    checkpoint = str(tmp_path / 'migration.checkpoint.json')
    first_args = SimpleNamespace(checkpoint=checkpoint, resume=False, execute=True,
        verify_only=False, expected_count=3, limit=2, batch_size=1)
    first = await migration._run(first_args)
    assert first['processed'] == 2 and first['complete'] is False
    assert migration._read_json(tmp_path / 'migration.checkpoint.json')['next_index'] == 2

    resume_args = SimpleNamespace(checkpoint=checkpoint, resume=True, execute=True,
        verify_only=False, expected_count=3, limit=None, batch_size=1)
    second = await migration._run(resume_args)
    state = migration._read_json(tmp_path / 'migration.checkpoint.json')
    assert second['processed'] == 1 and second['complete'] is True
    assert state['next_index'] == 3 and state['archived_total'] == 3
    assert len(client.objects) == 6
    assert all(all(sql.lstrip().upper().startswith('SELECT') for sql in conn.sql)
               for conn in connections)
    assert all(all(flag is True for flag in conn.readonly_flags)
               for conn in connections)
