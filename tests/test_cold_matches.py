from datetime import datetime, timezone
import pytest
from src.api.main import app
from src.api.cold_matches import cold_match_factory
from .test_cold_history import fixture_reader
from .test_api import seed


@pytest.fixture
def archive(monkeypatch):
    monkeypatch.setenv('HISTORY_ARCHIVE_ENABLED', 'true')
    def install(**kwargs):
        reader, requests = fixture_reader(**kwargs)
        app.dependency_overrides[cold_match_factory] = lambda: lambda: reader
        return reader, requests
    yield install
    app.dependency_overrides.pop(cold_match_factory, None)


async def test_existing_detail_and_odds_read_cold_with_empty_hot_database(api, archive):
    reader, requests = archive(redirect=True)
    for path in ('/api/matches/7', '/api/matches/7/odds'):
        response = await api.get(path)
        assert response.status_code == 200, response.text
        assert response.headers['x-history-revision'] == '2'
        assert response.headers['x-history-snapshot'] == reader.digest
        assert response.headers['x-history-source-parity'] == 'unverified'
        assert response.headers['x-history-cleanup-authorized'] == 'false'
        odds = response.json() if path.endswith('odds') else response.json()['odds']
        assert odds[0]['closing']['home'] == 1.8
        assert odds[0]['raw'] == {}
        assert 'server-token' not in response.text
        assert 'must not reach browser' not in response.text
    assert requests


async def test_revision_snapshot_and_no_match(api, archive):
    reader, _ = archive()
    for path in ('/api/matches/7', '/api/matches/7/odds'):
        response = await api.get(path, params={'board_version': 'a'*64, 'revision': 1, 'snapshot': reader.digest})
        assert response.status_code == 200
        assert response.headers['x-history-revision'] == '1'
        assert (await api.get(path, params={'snapshot': 'b'*64})).status_code == 503
        assert (await api.get(path, params={'revision': 99})).status_code == 404
        assert (await api.get(path, params={'revision': 0})).status_code == 422
    assert (await api.get('/api/matches/999')).status_code == 404


@pytest.mark.parametrize('kwargs', [{'broken': True}, {'outage': True}, {'duplicate': True}])
async def test_broken_archive_returns_explicit_503(api, archive, kwargs):
    archive(**kwargs)
    for path in ('/api/matches/7', '/api/matches/7/odds'):
        response = await api.get(path)
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'history_archive_unavailable'


async def test_hot_record_wins_without_archive_request(api, db, archive):
    await seed(db)
    match_id = 7  # Same identity also exists in the encrypted fixture.
    _, requests = archive(outage=True)
    for path in (f'/api/matches/{match_id}', f'/api/matches/{match_id}/odds'):
        response = await api.get(path)
        assert response.status_code == 200
        assert 'x-history-source' not in response.headers
    assert requests == []


async def test_disabled_preserves_missing_match_contract(api, monkeypatch):
    monkeypatch.delenv('HISTORY_ARCHIVE_ENABLED', raising=False)
    for path in ('/api/matches/7', '/api/matches/7/odds'):
        assert (await api.get(path)).status_code == 404
        assert (await api.get(path, params={'revision': 1})).status_code == 503


@pytest.mark.parametrize('case', ['pending', 'missing', 'score', 'revision', 'future', 'today', 'unresolved'])
async def test_active_or_unproven_records_cannot_be_cold_fallback(api, archive, case):
    def mutate(record):
        if case == 'pending': record['evidence'][0]['outcome'] = 'pending'
        if case == 'missing': record['evidence'] = []
        if case == 'score': record['evidence'][0]['home_score'] = 8
        if case == 'revision': record['evidence'][0]['revision'] = 100
        if case == 'unresolved': record['match']['status'] = 'scheduled'
        if case in ('today', 'future'):
            day = datetime.now(timezone.utc).date().isoformat() if case == 'today' else '2099-01-01'
            record['day'] = day
            record['match']['kickoff_at'] = day + 'T12:00:00Z'
    archive(mutate=mutate)
    assert (await api.get('/api/matches/7')).status_code == 503


async def test_multiple_boards_require_explicit_version(api, archive):
    def mutate(record):
        if record['revision'] == 1: record['board_version'] = 'b'*64
    archive(mutate=mutate)
    assert (await api.get('/api/matches/7')).status_code == 503
    response = await api.get('/api/matches/7', params={'board_version': 'b'*64})
    assert response.status_code == 200
    assert response.headers['x-history-revision'] == '1'


async def test_existing_dashboard_reads_encrypted_archive_and_displays_outage(api, archive):
    from playwright.async_api import async_playwright
    archive(mutate=lambda record: record['match']['odds'][0].update(bookmaker='Crown'))
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        async def route(request):
            from urllib.parse import urlsplit
            url = urlsplit(request.request.url)
            if url.hostname != 'app.test':
                return await request.fulfill(status=404, body='')
            if url.path.startswith('/api/') and url.path not in ('/api/matches/7', '/api/matches/7/odds'):
                return await request.fulfill(status=503, json={'detail': 'unavailable'})
            result = await api.get(url.path + ('?' + url.query if url.query else ''))
            await request.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)
        await page.route('**/*', route)
        await page.goto('http://app.test/')
        await page.wait_for_function('Boolean(globalThis.BetAppDashboard)')
        await page.evaluate('BetAppDashboard.selectMatch(7)')
        assert await page.locator('#detail-title').inner_text() == 'Home vs Away'
        assert 'Revizyon 2' in await page.locator('#detail-status').inner_text()
        assert 'doğrulanmadı' in await page.locator('#detail-status').inner_text()
        assert '1.8' in await page.locator('#odds-1x2-latest').inner_text()
        archive(broken=True)
        await page.evaluate('BetAppDashboard.selectMatch(7)')
        assert 'Geçmiş arşivi doğrulanamıyor' in await page.locator('#detail-status').inner_text()
        await browser.close()


async def test_latest_revision_is_not_chosen_by_corrected_kickoff_date(api, archive):
    def mutate(record):
        if record['revision'] == 1:
            record['day'] = '2026-10-31'
            record['match']['kickoff_at'] = '2026-10-31T12:00:00Z'
    archive(mutate=mutate)
    response = await api.get('/api/matches/7')
    assert response.status_code == 200
    assert response.headers['x-history-revision'] == '2'


async def test_standalone_match_dossier_uses_archived_identity_and_evidence(api, archive):
    reader, _ = archive()
    response = await api.get('/api/matches/7/dossier')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['archive_only'] is True
    assert data['match']['id'] == 7
    assert data['archive'] == {
        'snapshot': reader.digest, 'board_version': 'a' * 64, 'revision': 2,
        'source_parity': 'unverified', 'cleanup_authorized': False,
    }
    assert data['evidence'][0]['revision'] == 2
    assert data['odds'][0]['closing']['home'] == 1.8
    assert 'server-token' not in response.text
    assert 'must not reach browser' not in response.text


async def test_standalone_match_dossier_honors_explicit_revision(api, archive):
    reader, _ = archive()
    response = await api.get('/api/matches/7/dossier', params={
        'board_version': 'a' * 64, 'revision': 1, 'snapshot': reader.digest,
    })
    assert response.status_code == 200, response.text
    assert response.headers['x-history-revision'] == '1'
    assert response.json()['evidence'][0]['revision'] == 1


async def test_standalone_match_dossier_shows_archive_limitations_in_chromium(api, archive):
    from pathlib import Path
    from playwright.async_api import async_playwright, expect
    archive()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        async def route(request):
            from urllib.parse import urlsplit
            url = urlsplit(request.request.url)
            if url.hostname != 'app.test':
                return await request.fulfill(status=404, body='')
            if url.path == '/api/matches/7/dossier':
                result = await api.get(url.path + ('?' + url.query if url.query else ''))
                return await request.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)
            if url.path == '/match-page.js':
                return await request.fulfill(body=Path('src/api/match-page.js').read_text(encoding='utf-8'),
                                             content_type='application/javascript')
            if url.path == '/match-page.css':
                return await request.fulfill(body=Path('src/api/match-page.css').read_text(encoding='utf-8'),
                                             content_type='text/css')
            if url.path == '/match/7':
                result = await api.get(url.path)
                return await request.fulfill(status=result.status_code, headers=dict(result.headers), body=result.content)
            await request.fulfill(status=404, body='')
        await page.route('**/*', route)
        await page.goto('http://app.test/match/7?board_version=' + 'a' * 64 + '&revision=1#predictions')
        await expect(page.locator('#hero h1')).to_have_text('Home')
        await expect(page.locator('#feedback')).to_contain_text('Kayıtlar yüklendi')
        await expect(page.locator('#panel-predictions')).to_contain_text('Arşivlenmiş sonuç kanıtları')
        await expect(page.locator('#panel-predictions')).to_contain_text('Dondurulmuş ilk tahmin yayını')
        await expect(page.locator('#panel-predictions')).to_contain_text('1x2')
        await expect(page.locator('#panel-predictions tbody tr')).to_contain_text('1')
        assert await page.locator('#panel-predictions img').count() == 0
        await browser.close()
