import os
from pathlib import Path
import subprocess
from urllib.parse import quote
from types import SimpleNamespace

import pytest

from src.archive import source_preflight as p


def url(tmp_path):
    cert = tmp_path / 'root.pem'
    cert.write_text('fixture CA file, not a real certificate')
    return ('postgresql://kupon_archive_reader:secret-sentinel@example.invalid/db'
            '?sslmode=verify-full&sslrootcert=' + quote(str(cert), safe=''))


@pytest.mark.parametrize('change', [
    lambda s: s.replace('verify-full', 'require'),
    lambda s: s.replace('kupon_archive_reader', 'production_owner'),
    lambda s: s + '&options=-cdefault_transaction_read_only=off',
    lambda s: s + '&service=production',
    lambda s: s + '&sslmode=disable',
    lambda s: s.replace('postgresql://', 'postgresql+asyncpg://'),
])
def test_rejects_unsafe_source_configuration(tmp_path, change):
    with pytest.raises(ValueError):
        p.source_url(change(url(tmp_path)))


def test_local_check_never_connects_or_leaks(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('RELATIONAL_SOURCE_DATABASE_URL', url(tmp_path))
    monkeypatch.setattr('sys.argv', ['preflight'])
    monkeypatch.setattr(p.psycopg, 'connect', lambda *a, **k: pytest.fail('unexpected connection'))
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert p.main() == 1
    output = capsys.readouterr().out
    assert 'secret-sentinel' not in output and 'example.invalid' not in output
    assert 'PENDING full_source_parity' in output
    assert 'BLOCKED local_pg18_image' in output


def test_source_exception_is_redacted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('RELATIONAL_SOURCE_DATABASE_URL', url(tmp_path))
    monkeypatch.setattr('sys.argv', ['preflight', '--check-source'])
    def fail(*args, **kwargs):
        raise RuntimeError('secret-sentinel: database error with SQL and private host')
    monkeypatch.setattr(p.psycopg, 'connect', fail)
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    assert p.main() == 1
    output = capsys.readouterr().out
    assert 'secret-sentinel' not in output
    assert 'BLOCKED source_access_and_role_gate' in output


def test_docker_probe_uses_readonly_ca_and_no_secret_in_arguments(tmp_path, monkeypatch):
    parsed = p.source_url(url(tmp_path))
    monkeypatch.setenv('PGSERVICE', 'unwanted')
    def run(command, **kwargs):
        assert 'secret-sentinel' not in str(command)
        assert kwargs['env']['PGPASSWORD'] == 'secret-sentinel'
        assert 'PGSERVICE' not in kwargs['env']
        assert '--pull=never' in command
        assert any(str(item).endswith(':ro') for item in command)
        assert 'BEGIN READ ONLY' in command[-1]
        return SimpleNamespace(returncode=0, stdout=b't\n')
    monkeypatch.setattr(p.subprocess, 'run', run)
    assert p.docker_tls_check(parsed)


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 wrapper')
def test_powershell_wrapper_sanitizes_missing_configuration():
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != 'RELATIONAL_SOURCE_DATABASE_URL'}
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                             '-File', str(root / 'scripts/test-relational-source.ps1')],
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 1
    assert 'BLOCKED source_configuration' in result.stdout
    assert not result.stderr
