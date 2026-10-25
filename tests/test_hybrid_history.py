"""Synthetic observations only; no network, Telegram, NVIDIA or provider exports."""
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from src.models import Match, MatchStatistics, Season, Team, League
from src.analytics import history_cache as cache
from src.analytics import season_archive as archive
from src.match_views import active_bounds, upcoming_predicate

NOW = datetime(2026, 10, 24, 20, 50, tzinfo=timezone.utc)


async def seed(db):
    async with db.session() as s:
        league = await s.scalar(select(League.id).limit(1))
        teams = [Team(external_id=980001 + i, name=f'Private test club {i}') for i in range(2)]
        s.add_all(teams); await s.flush()
        rows = []
        for i, season_name in enumerate(archive.SEASONS):
            season = Season(league_id=league, season_name=season_name)
            s.add(season); await s.flush()
            for j in range(25):
                kickoff = datetime(2024 + i, 8, 1, tzinfo=timezone.utc) + timedelta(days=j)
                m = Match(external_match_id=980000 + i * 100 + j, league_id=league, season_id=season.id,
                    round=j + 1, home_team_id=teams[j % 2].id, away_team_id=teams[(j+1) % 2].id,
                    kickoff_at=kickoff, status='finished', ft_home=2, ft_away=1, ht_home=1, ht_away=0,
                    created_at=kickoff, updated_at=kickoff+timedelta(hours=3),
                    last_scraped_at=kickoff+timedelta(hours=3), raw={'private_provider_payload': 'never export'})
                s.add(m); rows.append(m)
        await s.flush()
        for m in rows:
            s.add(MatchStatistics(match_id=m.id, is_final=True, collection_status='available',
                home_corners=5, away_corners=3, home_yellow_cards=1, away_yellow_cards=2,
                home_red_cards=0, away_red_cards=0, updated_at=m.updated_at, raw={'private': True}))
        target = Match(external_match_id=989999, league_id=league, season_id=season.id, round=30,
            home_team_id=teams[0].id, away_team_id=teams[1].id, kickoff_at=NOW+timedelta(hours=1),
            status='scheduled', created_at=NOW-timedelta(days=1), updated_at=NOW-timedelta(days=1), raw={})
        s.add(target); await s.commit()
        return league, target.id, rows[0].id


@pytest.fixture
def hybrid(monkeypatch, tmp_path):
    monkeypatch.setenv('HYBRID_HISTORY_ENABLED', 'true')
    monkeypatch.setenv('SEASON_AGGREGATE_DIR', str(tmp_path / 'aggregates'))
    return tmp_path / 'aggregates'


def test_midnight_calendar_and_rolling_hours_are_distinct(hybrid):
    # 23:50 in Istanbul: end is exclusive Oct 28 00:00, not now+72h.
    start, end = active_bounds(NOW)
    assert start == datetime(2026, 10, 23, 21, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 27, 21, tzinfo=timezone.utc)
    assert end - NOW == timedelta(hours=72, minutes=10)
    assert active_bounds(NOW+timedelta(minutes=10)) == (start+timedelta(days=1), end+timedelta(days=1))


async def test_active_window_excludes_end_and_preserves_old_results(db, hybrid):
    league, target, old = await seed(db)
    _, end = active_bounds(NOW)
    async with db.session() as s:
        match = await s.get(Match, target)
        match.kickoff_at = end
        await s.commit()
    async with db.session() as s:
        assert not list(await s.scalars(select(Match.id).where(upcoming_predicate(NOW))))
        assert (await s.get(Match, old)).status == 'finished'
        assert len(await cache.historical_rows(s, league, NOW, target)) == 75


async def test_history_matches_legacy_models_and_has_no_duplicates(db, hybrid, monkeypatch):
    league, target, old = await seed(db)
    from src.analytics.service import statistics, _cache
    async def calculate():
        _cache.clear()
        async with db.session() as s:
            m = await s.get(Match, target)
            return await statistics(s, m, {'competition_type': 'club'}, NOW)
    with monkeypatch.context() as m:
        m.setenv('HYBRID_HISTORY_ENABLED', 'false')
        original = await calculate()
    cached = await calculate()
    def evidence(value):
        if isinstance(value, dict):return {k:evidence(v) for k,v in value.items() if k != 'duration_ms'}
        if isinstance(value, list):return [evidence(v) for v in value]
        return value
    assert evidence(cached) == evidence(original)
    async with db.session() as s:
        rows = await cache.historical_rows(s, league, NOW, target)
        assert len(rows) == len({r[0].id for r in rows}) == 75
        assert {r[0].season_id for r in rows} == {1, 2, 3}


async def test_corrections_and_live_to_finished_invalidate(db, hybrid):
    league, target, old = await seed(db)
    async with db.session() as s:
        assert len(await cache.historical_rows(s, league, NOW, target)) == 75
    async with db.session() as s:
        m = await s.get(Match, old)
        m.ft_home = 7; m.updated_at = NOW+timedelta(seconds=1)
        live = await s.get(Match, target)
        live.kickoff_at = NOW-timedelta(hours=4); live.status = 'live'
        await s.commit()
    async with db.session() as s:
        rows = await cache.historical_rows(s, league, NOW, -1)
        assert old not in {r[0].id for r in rows}  # late correction cannot enter old as-of
        assert target not in {r[0].id for r in rows}
    async with db.session() as s:
        live = await s.get(Match, target)
        live.status = 'finished'; live.ft_home = 3; live.ft_away = 2
        live.updated_at = NOW+timedelta(seconds=2)
        await s.commit()
    async with db.session() as s:
        rows = await cache.historical_rows(s, league, NOW+timedelta(seconds=3), -1)
        assert len(rows) == 76
        assert next(m.ft_home for m, *_ in rows if m.id == old) == 7


async def test_time_advancing_across_completion_buffer_and_expiry(db, hybrid, monkeypatch):
    league, target, old = await seed(db)
    async with db.session() as s:
        m = await s.get(Match, old)
        m.kickoff_at = NOW-timedelta(hours=3)+timedelta(seconds=1)
        m.created_at = m.updated_at = m.last_scraped_at = NOW-timedelta(minutes=1)
        await s.commit()
    async with db.session() as s:
        assert len(await cache.historical_rows(s, league, NOW, target)) == 74
    async with db.session() as s:
        assert len(await cache.historical_rows(s, league, NOW+timedelta(seconds=2), target)) == 75
    with monkeypatch.context() as m:
        m.setattr(cache.time, 'monotonic', lambda: float('inf'))
        async with db.session() as s:
            assert len(await cache.historical_rows(s, league, NOW+timedelta(hours=72), target)) == 75


async def test_saturated_cache_uses_bounded_fallback(db, hybrid, monkeypatch):
    league, target, _ = await seed(db)
    monkeypatch.setattr(cache, 'MAX_ROWS', 10)
    async with db.session() as s:
        assert await cache.historical_rows(s, league, NOW, target) is None
    from src.analytics.match_statistics import additional_statistics
    async with db.session() as s:
        await additional_statistics(s, await s.get(Match, target), NOW)
        assert len(s.info['recommendation_count_history'][(league, NOW)]) == 10


async def test_empty_db_and_flag_off(db, hybrid, monkeypatch):
    async with db.session() as s:
        assert await cache.historical_rows(s, 1, NOW, -1) == []
        rows = await archive.aggregate_rows(s, NOW)
        assert all(row['source_matches'] == '0' for row in rows)
    monkeypatch.setenv('HYBRID_HISTORY_ENABLED', 'false')
    async with db.session() as s:
        assert await cache.historical_rows(s, 1, NOW, -1) is None
        assert await archive.read_or_query(s, NOW) == {'enabled': False, 'source': 'disabled', 'seasons': []}
    assert active_bounds(NOW) == (NOW, NOW+timedelta(days=7))


async def test_old_picks_settle_and_cannot_be_replaced_retrospectively(db, hybrid):
    league, target, old = await seed(db)
    from src.models import PredictionSnapshot, PredictionSettlement
    from src.analytics.performance import capture
    from src.analytics.settlements import settle_batch
    pick = dict(market='result', selection='home', probability=.7)
    async with db.session() as s:
        m = await s.get(Match, old)
        s.add(PredictionSnapshot(match_id=old, league_id=league,
            captured_at=cache.aware(m.kickoff_at)-timedelta(hours=1), picks=[pick], model_version='test-original'))
        await s.commit()
    async with db.session() as s:
        await settle_batch(s)
        await s.commit()
    async with db.session() as s:
        assert not await capture(s, await s.get(Match, old), [dict(pick, probability=.99)], NOW)
        snapshot = await s.get(PredictionSnapshot, old)
        assert snapshot.picks == [pick] and snapshot.model_version == 'test-original'
        assert await s.scalar(select(PredictionSettlement.outcome).where(PredictionSettlement.match_id == old)) == 'won'
        assert await s.get(Match, old) is not None


async def test_uncommitted_history_cannot_enter_shared_cache(db, hybrid):
    league, target, old = await seed(db)
    async with db.session() as s:
        m = await s.get(Match, old)
        m.ft_home = 10
        await s.flush()
        assert await cache.historical_rows(s, league, NOW, target) is None
        await s.rollback()
    async with db.session() as s:
        rows = await cache.historical_rows(s, league, NOW, target)
        assert next(m.ft_home for m, *_ in rows if m.id == old) == 2


async def test_history_api_and_model_window_metadata(api, db, hybrid, monkeypatch):
    league, target, old = await seed(db)
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    response = await api.get('/api/history/seasons')
    assert response.status_code == 200
    assert response.json()['source'] == 'postgresql_aggregate'
    response = await api.get('/api/predictions')
    assert response.status_code == 200
    assert response.json()['cache']['active_window']['semantics'] == 'today_plus_3_calendar_days'


async def test_calendar_window_browser_label(api, hybrid, monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def forward(route):
            response = await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code, body=response.content,
                content_type=response.headers.get('content-type', 'application/json'))
        await page.route('http://test/**', forward)
        await page.goto('http://test/')
        await page.locator('.desktop-nav [data-page-link=predictions]').click()
        await page.evaluate("document.querySelector('#analysis-advanced').open = true")
        await page.locator('#upcoming-board').click()
        await expect(page.locator('#prediction-window')).to_contain_text('Bugün + sonraki 3 gün')
        if __import__('os').getenv('CAPTURE_HYBRID_SCREENSHOT') == 'true':
            from pathlib import Path
            root = Path('docs/hybrid-history-130')
            root.mkdir(parents=True, exist_ok=True)
            await expect(page.locator('#upcoming-board')).to_have_text('Bugün + sonraki 3 gün')
            await page.locator('#upcoming-board').scroll_into_view_if_needed()
            await page.screenshot(path=str(root / 'empty-calendar-window.png'))
        assert errors == []
        await browser.close()


async def test_csv_seasons_deterministic_atomic_and_allowlisted(db, hybrid):
    await seed(db)
    async with db.session() as s:
        rows = await archive.aggregate_rows(s, NOW)
        again = await archive.aggregate_rows(s, NOW)
    assert rows == again
    archive.publish(hybrid, rows)
    before = {p.name: p.read_bytes() for p in hybrid.glob('*.csv')}
    archive.publish(hybrid, again)
    assert before == {p.name: p.read_bytes() for p in hybrid.glob('*.csv')}
    for row in rows:
        assert row['finished_matches'] == row['scored_matches'] == '25'
        assert row['home_goals'] == '50' and row['away_goals'] == '25'
        loaded = archive.read_one(hybrid, row['season'], row['source_scope'], NOW)
        assert loaded == row
        data = (hybrid / f"season-{row['season']}.csv").read_text()
        assert 'private' not in data.lower() and '9800' not in data
        assert set(loaded) == set(archive.FIELDS)
    assert not list(hybrid.glob('.season-*'))


@pytest.mark.parametrize('damage', ['missing', 'hash', 'oversized', 'schema', 'wrong_db', 'expired', 'future'])
async def test_missing_corrupt_or_wrong_database_falls_back(db, hybrid, damage):
    await seed(db)
    async with db.session() as s:
        rows = await archive.aggregate_rows(s, NOW)
    archive.publish(hybrid, rows)
    path = hybrid / 'season-2024-2025.csv'
    if damage == 'missing':path.unlink()
    elif damage == 'hash':path.write_text(path.read_text().replace(',50,25', ',99,25'))
    elif damage == 'oversized':path.write_bytes(b'x' * (archive.MAX_BYTES+1))
    elif damage == 'schema':path.write_text(path.read_text().replace('home_goals', 'home_team_id'))
    elif damage == 'wrong_db':
        rows[0]['source_scope'] = 'wrong'; rows[0]['content_hash'] = archive.digest(rows[0]); archive.publish(hybrid, rows)
    when = NOW + (timedelta(minutes=11) if damage == 'expired' else timedelta(seconds=-1) if damage == 'future' else timedelta())
    async with db.session() as s:
        result = await archive.read_or_query(s, when)
    assert result['source'] == 'postgresql_aggregate'
    assert len(result['seasons']) == 3


async def test_warm_csv_requires_no_remote_read_and_restart_recovers(db, hybrid):
    await seed(db)
    assert await archive.refresh(db, NOW)
    queries = []
    def count(conn, cursor, statement, parameters, context, many):queries.append(statement)
    event.listen(db.engine.sync_engine, 'before_cursor_execute', count)
    try:
        async with db.session() as s:
            assert (await archive.read_or_query(s, NOW))['source'] == 'local_csv'
        assert queries == []
        for p in hybrid.glob('*.csv'):p.unlink()  # Ephemeral Render disk after restart.
        async with db.session() as s:
            assert (await archive.read_or_query(s, NOW))['source'] == 'postgresql_aggregate'
        assert len(queries) == 1
    finally:
        event.remove(db.engine.sync_engine, 'before_cursor_execute', count)


async def test_read_only_disk_and_atomic_publish_failure_preserve_source(db, hybrid, monkeypatch):
    await seed(db)
    assert await archive.refresh(db, NOW)
    original = {p.name: p.read_bytes() for p in hybrid.glob('*.csv')}
    def fail(*args):raise PermissionError('read-only test disk')
    monkeypatch.setattr(archive.os, 'replace', fail)
    assert not await archive.refresh(db, NOW+timedelta(seconds=1))
    assert original == {p.name: p.read_bytes() for p in hybrid.glob('*.csv')}
    assert not list(hybrid.glob('.season-*'))


async def test_history_wire_volume_benchmark(db, hybrid, monkeypatch, capsys):
    league, target, old = await seed(db)
    from src.analytics.service import statistics, _cache
    statements = []
    def count(conn, cursor, statement, params, context, many):statements.append(statement)
    event.listen(db.engine.sync_engine, 'before_cursor_execute', count)
    try:
        totals = {}
        for flag in ('false', 'true'):
            monkeypatch.setenv('HYBRID_HISTORY_ENABLED', flag)
            statements.clear()
            for minute in range(3):
                _cache.clear()
                async with db.session() as s:
                    await statistics(s, await s.get(Match, target), {'competition_type': 'club'}, NOW+timedelta(minutes=minute))
            # Count transfers of history rowsets, excluding compact revision aggregates.
            reads = [sql for sql in statements if 'matches.status = ' in sql and 'count(' not in sql.lower()
                     and ('ORDER BY matches.kickoff_at DESC' in sql)]
            totals[flag] = len(reads)
        print(json.dumps({'legacy_history_transfers': totals['false'], 'hybrid_history_transfers': totals['true'],
            'synthetic_matches': 75, 'rebuilds': 3}, sort_keys=True))
        assert totals == {'false': 6, 'true': 1}
    finally:
        event.remove(db.engine.sync_engine, 'before_cursor_execute', count)
