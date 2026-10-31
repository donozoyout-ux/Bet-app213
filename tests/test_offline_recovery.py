import subprocess
from pathlib import Path
import psycopg
import pytest
from src.archive import offline_recovery as recovery
from src.archive.recovery_format import seal, unseal
import base64


def test_sqlstate_never_leaks_message():
    exc = psycopg.errors.RaiseException('password=secret SQL private payload')
    assert recovery.safe_failure('first_inventory', exc) == 'RECOVERY_INCOMPLETE stage=first_inventory sqlstate=P0001'
    assert recovery.safe_failure('first_pg18_restore', ValueError('secret')).endswith('sqlstate=unavailable')


def test_process_error_redacted(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'secret', stderr='pg_restore: error: ERROR: P0001: secret query')
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(recovery.CutoverBlocked) as error: recovery._docker(['exec'])
    assert recovery.safe_failure('first_pg18_restore', error.value).endswith('sqlstate=P0001')
    assert 'secret' not in str(error.value)


def test_cipher_authentication_and_identity():
    key = base64.b64encode(bytes(32)).decode()
    blob = seal(b'all history', key, 'backup', 'chunk-000000.enc')
    assert unseal(blob, key, 'backup', 'chunk-000000.enc') == b'all history'
    with pytest.raises(Exception): unseal(blob, key, 'other', 'chunk-000000.enc')
    with pytest.raises(Exception): unseal(blob[:-1]+bytes([blob[-1]^1]), key, 'backup', 'chunk-000000.enc')


def test_wrong_original_zip_fails_before_writes(tmp_path):
    archive = tmp_path/'original.zip'
    archive.write_bytes(b'not original')
    output = tmp_path/'dump'
    with pytest.raises(recovery.CutoverBlocked): recovery.load_offline_rescue(archive, '', output)
    assert archive.read_bytes() == b'not original'
    assert not output.exists()


def test_recovery_path_has_no_compaction():
    source = Path(recovery.__file__).read_text()
    assert 'DELETE FROM' not in source
    assert 'VACUUM' not in source
    assert 'apply_only_to_owned_local_verifier' not in source
    assert "'--pull=never'" in source


def test_inventory_order_and_duplicates():
    from src.archive.recovery_inventory import row_fingerprint
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query): pass
        def fetchmany(self, size):
            rows, self.rows = self.rows[:size], self.rows[size:]
            return [(r,) for r in rows]
    class Conn:
        def __init__(self, rows): self.rows = rows
        def cursor(self, **kwargs): return Cursor(self.rows)
    a = row_fingerprint(Conn(['a', 'b', 'a']), '')
    assert a == row_fingerprint(Conn(['b', 'a', 'a']), '')
    assert a != row_fingerprint(Conn(['b', 'a']), '')


@pytest.mark.parametrize('fail_at,expected_stage', [('restore','first_pg18_restore'),('inventory','first_inventory'),('parity','full_content_parity')])
def test_pipeline_failure_never_writes_success(tmp_path, monkeypatch, capsys, fail_at, expected_stage):
    from contextlib import contextmanager
    keyfile=tmp_path/'key'
    keyfile.write_text(base64.b64encode(bytes(32)).decode())
    root=tmp_path/'result'
    if recovery.os.name == 'nt': root.mkdir()
    monkeypatch.setattr('sys.argv',['offline','--zip','unused','--key-file',str(keyfile),'--output-root',str(root)])
    def decrypt(archive,key,output):
        output.write_bytes(b'synthetic')
        return {}
    @contextmanager
    def container(*args): yield 'owned',{}
    def restore(*args):
        if fail_at == 'restore': raise psycopg.errors.RaiseException('private SQL')
    count=0
    def inventory(*args):
        nonlocal count
        count+=1
        if fail_at == 'inventory': raise psycopg.errors.RaiseException('private row')
        return {'rows':count}
    monkeypatch.setattr(recovery,'load_offline_rescue',decrypt)
    monkeypatch.setattr(recovery,'ephemeral_pg18',container)
    monkeypatch.setattr(recovery,'_restore',restore)
    monkeypatch.setattr(recovery,'local_inventory',inventory)
    with pytest.raises(SystemExit): recovery.main()
    assert not (root/'verification.json').exists()
    output=capsys.readouterr().out
    assert 'stage='+expected_stage in output
    assert 'private' not in output
    assert 'LOCAL_RESTORE_VERIFIED' not in output
