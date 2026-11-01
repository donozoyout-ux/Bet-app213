import os
from pathlib import Path
import subprocess
from urllib.parse import quote
from types import SimpleNamespace

import pytest
import httpx
import base64

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
    env = {k: v for k, v in os.environ.items() if not k.startswith('RELATIONAL_')}
    env['RELATIONAL_ARCHIVE_ACTIVATED'] = 'true'
    env['RELATIONAL_SOURCE_DATABASE_URL'] = 'invalid-secret-sentinel'
    env['RELATIONAL_GITHUB_TOKEN'] = 'token-sentinel'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                             '-File', str(root / 'scripts/test-relational-source.ps1'), '-LocalOnly'],
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 1
    assert 'BLOCKED source_configuration' in result.stdout
    assert 'local_pg18_image' in result.stdout
    assert 'PASS upload_disabled' in result.stdout
    assert 'secret-sentinel' not in result.stdout and 'token-sentinel' not in result.stdout
    assert not result.stderr


@pytest.mark.parametrize('status,private,expected', [(200, True, True), (200, False, False), (403, True, False), (302, True, False)])
def test_github_probe_get_only_and_fail_closed(status, private, expected):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.github.com'
        assert 'token-sentinel' not in str(request.url)
        if request.url.path.endswith('/releases'):
            return httpx.Response(200, json=[])
        return httpx.Response(status, json=dict(full_name=p.ARCHIVE_REPOSITORY,
                              private=private, archived=False),
                              headers={'Location': 'https://untrusted.invalid/'})
    checks = p.github_checks(dict(RELATIONAL_GITHUB_TOKEN='token-sentinel',
                            RELATIONAL_GITHUB_REPOSITORY=p.ARCHIVE_REPOSITORY),
                            transport=httpx.MockTransport(respond))
    assert checks['github_repository_read'] is expected
    assert len(requests) == (2 if expected else 1)
    assert 'github_write_permission' not in checks


def test_github_wrong_repository_never_connects():
    def respond(request):
        pytest.fail('unexpected network request')
    assert p.github_checks(dict(RELATIONAL_GITHUB_TOKEN='secret',
                           RELATIONAL_GITHUB_REPOSITORY='other/repo'),
                           transport=httpx.MockTransport(respond)) == {'github_configuration': False}


def test_existing_reader_is_never_mutated(tmp_path, monkeypatch):
    executed = []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query):
            executed.append(query)
            return SimpleNamespace(fetchone=lambda: (True,))
        def rollback(self): executed.append('ROLLBACK')
    monkeypatch.setattr(p.psycopg, 'connect', lambda *a, **kw: Connection())
    reader = url(tmp_path)
    owner = reader.replace('kupon_archive_reader', 'owner')
    assert p.prepare_reader(owner, reader) == 'reader_existing_unchanged'
    assert len(executed) == 3
    assert not any(word in str(executed) for word in ('ALTER', 'GRANT', 'CREATE', 'PASSWORD'))


def test_owner_endpoint_mismatch_never_connects(tmp_path, monkeypatch):
    monkeypatch.setattr(p.psycopg, 'connect', lambda *a, **kw: pytest.fail('unexpected connection'))
    reader = url(tmp_path)
    with pytest.raises(ValueError):
        p.prepare_reader(reader.replace('kupon_archive_reader', 'owner').replace('example.invalid', 'other.invalid'), reader)


def test_github_exception_and_activated_archive_never_upload(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('RELATIONAL_SOURCE_DATABASE_URL', url(tmp_path))
    monkeypatch.setenv('RELATIONAL_ARCHIVE_ACTIVATED', 'true')
    monkeypatch.setenv('RELATIONAL_ENCRYPTION_KEY', base64.b64encode(b'x' * 32).decode())
    monkeypatch.setattr('sys.argv', ['preflight', '--check-github'])
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    def fail(*a, **kw): raise RuntimeError('token-sentinel secret-sentinel')
    monkeypatch.setattr(p, 'github_checks', fail)
    assert p.main() == 1
    output = capsys.readouterr().out
    assert 'token-sentinel' not in output and 'secret-sentinel' not in output
    assert 'PASS upload_disabled' in output
    assert 'BLOCKED github_access' in output
