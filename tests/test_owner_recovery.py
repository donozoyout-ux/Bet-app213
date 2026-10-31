"""Owner fallback and complete row parity: synthetic values only."""
import base64
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import subprocess

import pytest

from src.archive.full_recovery import row_fingerprint, client


class Cursor:
    def __init__(self, values): self.values=list(values)
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query): pass
    def fetchmany(self, limit):
        batch=self.values[:limit]; self.values=self.values[limit:]
        return [(value,) for value in batch]


class Connection:
    def __init__(self, values): self.values=values
    def cursor(self, **kwargs): return Cursor(self.values)


def test_full_row_hash_includes_duplicates_exact_numbers_ids_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.tempdir', str(tmp_path))
    rows=['{"id":1,"value":0.12345678901234567890123456789,"payload":{"x":"Türkçe"}}',
          '{"id":2,"value":null,"payload":{"x":"original"}}']
    first=row_fingerprint(Connection(rows), 'synthetic')
    assert first[0]==2
    assert row_fingerprint(Connection(list(reversed(rows))), 'synthetic')==first
    for changed in ([rows[0],rows[0]], [rows[0].replace('1,','3,',1),rows[1]],
                    [rows[0],rows[1].replace('original','changed')],
                    [rows[0].replace('6789','6788'),rows[1]]):
        assert row_fingerprint(Connection(changed), 'synthetic') != first
    assert row_fingerprint(Connection(rows+[rows[0]]), 'synthetic') != first
    assert not list(tmp_path.iterdir())  # Temporary sorter closes even on Windows.


def test_windows_restore_exec_targets_only_owned_pg18_container(tmp_path, monkeypatch):
    from src.archive import full_recovery as module
    owned='a'*64
    monkeypatch.setenv('RECOVERY_VERIFY_CONTAINER',owned)
    monkeypatch.setenv('RECOVERY_BACKUP_ID','synthetic')
    calls=[]
    def run(command, **kwargs):
        calls.append((command,kwargs))
        return subprocess.CompletedProcess(command,0,stdout='postgres:18 synthetic\n')
    monkeypatch.setattr(module.subprocess,'run',run)
    client('pg_restore',dict(host='127.0.0.1',port=5433,dbname='postgres',user='postgres',password='SYNTHETIC_PASSWORD'),
           tmp_path,'--dbname=postgres','/backup/recovered.dump')
    assert calls[-1][0][:2]==['docker','exec']
    assert owned in calls[-1][0] and '/tmp/recovered.dump' in calls[-1][0]
    assert calls[-1][1]['env']['PGPORT']=='5432'
    assert all('SYNTHETIC_PASSWORD' not in str(command) for command,_ in calls)
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a[0],0,stdout='postgres:18 another-owner\n'))
    with pytest.raises(ValueError,match='isolated_container_required'):
        client('pg_restore',{},tmp_path)


@pytest.mark.skipif(os.name!='nt', reason='Windows owner-operated launcher')
def test_owner_key_creation_exact_size_no_log_disclosure_or_overwrite(tmp_path):
    executable=shutil.which('powershell.exe')
    if not executable: pytest.skip('PowerShell unavailable')
    script=Path('scripts/owner-full-recovery.ps1').resolve()
    key=tmp_path/'private-owner-key'/'recovery.key'
    command=[executable,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),'-Mode','InitKey','-KeyFile',str(key)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode==0, result.stdout
    value=key.read_text().strip()
    assert len(base64.b64decode(value,validate=True))==32
    assert value not in result.stdout+result.stderr
    again=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert again.returncode!=0 and key.read_text().strip()==value


def test_full_recovery_sql_smoke_is_not_site_readthrough_claim():
    source=Path('src/archive/full_recovery.py').read_text()
    assert 'application_read_through_verified=False' in source
    assert 'sql_smoke=checks' in source
    launcher=Path('scripts/owner-full-recovery.ps1').read_text(encoding='utf-8-sig')
    assert 'KeyRetainedOffline' in launcher
    assert 'Read-Host' in launcher and '-AsSecureString' in launcher
    assert 'ARCHIVE_ENCRYPTION_KEY' in launcher
    assert 'Set-Secret' not in launcher and 'gh secret set' not in launcher


def test_owner_source_preflight_never_echoes_bad_credentials(monkeypatch, capsys):
    import sys
    from src.archive import owner_recovery
    monkeypatch.setenv('ARCHIVE_DATABASE_URL', 'postgresql://user:PRIVATE_VALUE@invalid.example/db')
    monkeypatch.setattr(sys, 'argv', ['owner_recovery', 'check-source'])
    assert owner_recovery.main() == 1
    assert capsys.readouterr().out == 'OWNER_PREFLIGHT_FAILED: check-source\n'


async def test_owner_archive_preflight_closes_without_mutation(monkeypatch):
    from src.archive import owner_recovery
    monkeypatch.setenv('ARCHIVE_ENCRYPTION_KEY', base64.b64encode(b'x'*32).decode())
    monkeypatch.setenv('ARCHIVE_GITHUB_TOKEN', 'synthetic-token')
    calls=[]
    class Store:
        def __init__(self, *args, **kwargs): pass
        async def inventory(self): calls.append('read-private-inventory')
        async def close(self): calls.append('close')
    monkeypatch.setattr(owner_recovery, 'RecoveryStore', Store)
    await owner_recovery.check_archive()
    assert calls == ['read-private-inventory', 'close']
