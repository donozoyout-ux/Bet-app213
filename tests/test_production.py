"""Render's Linux startup command and degraded/connected HTTP contract."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx
import pytest
from src.db import Database
from scripts.smoke_test import smoke_test

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform == 'win32', reason='Gunicorn requires POSIX; exercised on Linux in CI')
@pytest.mark.parametrize('url', ['', 'postgres://test-only:test-only@127.0.0.1:1/unavailable',
    pytest.param(os.getenv('TEST_POSTGRES_URL', ''), marks=pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='TEST_POSTGRES_URL not configured'))])
def test_render_gunicorn_boot_with_unavailable_database(url, tmp_path):
    connected = bool(url and ':1/' not in url)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'APP_ENV': 'production',
           'DATABASE_URL': url, 'SCRAPER_WORKER_ENABLED': 'false' if connected else 'true',
           'AUTO_BACKFILL_ON_EMPTY': 'false' if connected else 'true',
           'SCRAPER_API_TOKEN': '', 'PORT': str(port)}
    # Use render.yaml's command literally, including shell expansion of $PORT.
    command = next(line.split('startCommand:', 1)[1].strip() for line in (ROOT / 'render.yaml').read_text().splitlines() if 'startCommand:' in line)
    assert command.endswith('src.api.main:app')
    log_file = tmp_path / 'gunicorn.log'
    with log_file.open('w+') as output:
        proc = subprocess.Popen(command, shell=True, cwd=tmp_path, env=env,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            base = f'http://127.0.0.1:{port}'
            while True:
                assert proc.poll() is None, log_file.read_text()
                try:
                    result = smoke_test(base, timeout=2)
                    if connected and not result['database_connected']:
                        raise AssertionError('Waiting for PostgreSQL initialization')
                    break
                except (httpx.HTTPError, AssertionError):
                    if time.monotonic() >= deadline:
                        pytest.fail(log_file.read_text())
                    time.sleep(0.1)
            assert result['database_connected'] is connected
            with httpx.Client(base_url=base) as client:
                assert client.get('/api/matches').status_code == (200 if connected else 503)
                diagnostics = client.get('/api/diagnostics').json()
                assert diagnostics['app'] == 'ok'
                assert diagnostics['database_configured'] is bool(url)
                assert diagnostics['database_connected'] is connected
        finally:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)


@pytest.mark.parametrize('prefix', ['postgres://', 'postgresql://', 'postgresql+asyncpg://'])
async def test_postgres_url_normalization_preserves_credentials(prefix):
    db = Database(prefix + 'user:p%40ss@localhost:5432/example?ssl=require')
    try:
        assert db.engine.url.drivername == 'postgresql+asyncpg'
        assert db.engine.url.password == 'p@ss'
        assert db.engine.url.query['ssl'] == 'require'
    finally:
        await db.close()


async def test_diagnostics_connected_and_disconnected(api, db, monkeypatch):
    import src.api.routes as routes
    diagnostics = (await api.get('/api/diagnostics')).json()
    assert diagnostics['database_connected']
    assert diagnostics['total_matches'] == diagnostics['total_odds'] == 0
    assert set(diagnostics) == {'app', 'database_configured', 'database_connected', 'worker_enabled',
                                'auto_backfill_enabled', 'latest_job_status', 'total_matches', 'total_odds'}
    monkeypatch.setattr(routes, 'settings', replace(routes.settings, scraper_token='SECRET_MARKER', database_url='SECRET_MARKER'))
    async def fail(*args):
        raise RuntimeError('SECRET_MARKER')
    monkeypatch.setattr(routes, 'database_summary', fail)
    response = await api.get('/api/diagnostics')
    assert response.status_code == 200
    assert not response.json()['database_connected']
    assert 'SECRET_MARKER' not in response.text


async def test_background_supervisor_recovers_unexpected_failure(monkeypatch):
    import src.api.main as main
    import src.jobs.worker as worker
    calls = []
    recovered = asyncio.Event()
    class RecoveringDatabase:
        ready = False
        async def initialize(self):
            calls.append('initialize')
            if len(calls) == 1:
                raise RuntimeError('unexpected initialization failure')
            self.ready = True
    db = RecoveringDatabase()
    async def enqueue(*args):
        return None
    async def work():
        calls.append('work')
        if calls.count('work') == 1:
            raise RuntimeError('unexpected scraper failure')
        recovered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(main, 'database', db)
    monkeypatch.setattr(main, 'settings', replace(main.settings, worker_enabled=True))
    monkeypatch.setattr(main, 'BACKGROUND_RETRY_SECONDS', 0)
    monkeypatch.setattr(worker, 'enqueue_initial_backfill', enqueue)
    monkeypatch.setattr(worker, 'work', work)
    task = asyncio.create_task(main.supervise_database())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        assert calls.count('initialize') == 3
        assert calls.count('work') == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_verification_cli_reads_schema_without_mutation(db, monkeypatch, capsys):
    import src.jobs.verify as verify
    monkeypatch.setattr(verify, 'database', db)
    assert await verify.verify() == 0
    import json
    report = json.loads(capsys.readouterr().out)
    assert report['schema_ok'] and report['database_connected']
    assert report['league_count'] == 1
    assert report['total_matches'] == report['total_odds'] == 0
    assert report['latest_job'] is None


async def test_verification_cli_unconfigured_is_nonzero(monkeypatch, capsys):
    import src.jobs.verify as verify
    monkeypatch.setattr(verify, 'database', Database(''))
    assert await verify.verify() == 1
    assert 'database_url' not in capsys.readouterr().out.lower()
