"""Independent first-half and second-half goal market tests:
calculations, subtraction, missing/impossible values, prematch constraints,
splits, quality gates, settlement, durable board cache, API and Playwright browser UI.
"""
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.analytics.half_goals import is_valid_half_match, get_half_scores, half_goal_summary, half_goal_prediction
from src.analytics.performance import capture, settle
from src.analytics.recommendations import build_candidates, select_recommendations
from src.analytics.service import statistics
from src.db import Database
from src.models import Match, PredictionSnapshot, PredictionBoard
from tests.test_predictions import seed, NOW


def test_correct_first_half_and_second_half_calculations():
    # Regular match: HT 1-0, FT 3-1
    m1 = SimpleNamespace(status='finished', ht_home=1, ht_away=0, ft_home=3, ft_away=1)
    assert is_valid_half_match(m1) is True
    # First half
    h1, a1 = get_half_scores(m1, 'first')
    assert h1 == 1 and a1 == 0
    # Second half subtraction: home = 3 - 1 = 2, away = 1 - 0 = 1
    h2, a2 = get_half_scores(m1, 'second')
    assert h2 == 2 and a2 == 1

    # Goalless match: HT 0-0, FT 0-0
    m2 = SimpleNamespace(status='finished', ht_home=0, ht_away=0, ft_home=0, ft_away=0)
    assert is_valid_half_match(m2) is True
    assert get_half_scores(m2, 'first') == (0, 0)
    assert get_half_scores(m2, 'second') == (0, 0)


def test_missing_and_impossible_score_combinations_rejected():
    # Missing halftime scores
    m_no_ht = SimpleNamespace(status='finished', ht_home=None, ht_away=None, ft_home=2, ft_away=1)
    assert is_valid_half_match(m_no_ht) is False
    assert get_half_scores(m_no_ht, 'first') == (None, None)
    assert get_half_scores(m_no_ht, 'second') == (None, None)

    # Missing fulltime scores
    m_no_ft = SimpleNamespace(status='finished', ht_home=1, ht_away=0, ft_home=None, ft_away=None)
    assert is_valid_half_match(m_no_ft) is False
    assert get_half_scores(m_no_ft, 'first') == (None, None)

    # Negative scores
    m_neg = SimpleNamespace(status='finished', ht_home=-1, ht_away=0, ft_home=1, ft_away=0)
    assert is_valid_half_match(m_neg) is False

    # Impossible combinations: FT less than HT
    m_imp_home = SimpleNamespace(status='finished', ht_home=2, ht_away=0, ft_home=1, ft_away=0)
    assert is_valid_half_match(m_imp_home) is False
    assert get_half_scores(m_imp_home, 'second') == (None, None)

    m_imp_away = SimpleNamespace(status='finished', ht_home=0, ht_away=2, ft_home=1, ft_away=1)
    assert is_valid_half_match(m_imp_away) is False

    # Summary with corrupted/missing data records missing_reason and does not invent scores
    rows = [m_no_ht, m_imp_home, SimpleNamespace(status='finished', ht_home=1, ht_away=1, ft_home=2, ft_away=1, home_team_id=1, away_team_id=2)]
    s = half_goal_summary(rows, 1, 'first')
    assert s['sample_size'] == 1
    assert s['matches_considered'] == 3
    assert s['missing_total_sample_size'] == 2
    assert s['missing_data_reason'] == 'missing_scores'


def test_half_goal_settlement_rules():
    m = SimpleNamespace(status='finished', ht_home=1, ht_away=0, ft_home=2, ft_away=1)
    # 1st half: 1 + 0 = 1 total
    assert settle(m, None, {'selection': 'over_0_5', 'market': 'first_half_goals'}) == ('won', 1)
    assert settle(m, None, {'selection': 'under_0_5', 'market': 'first_half_goals'}) == ('lost', 1)
    assert settle(m, None, {'selection': 'over_1_5', 'market': 'first_half_goals'}) == ('lost', 1)
    assert settle(m, None, {'selection': 'under_1_5', 'market': 'first_half_goals'}) == ('won', 1)

    # 2nd half: (2 - 1) + (1 - 0) = 1 + 1 = 2 total
    assert settle(m, None, {'selection': 'over_0_5', 'market': 'second_half_goals'}) == ('won', 2)
    assert settle(m, None, {'selection': 'over_1_5', 'market': 'second_half_goals'}) == ('won', 2)
    assert settle(m, None, {'selection': 'under_1_5', 'market': 'second_half_goals'}) == ('lost', 2)
    assert settle(m, None, {'selection': 'over_2_5', 'market': 'second_half_goals'}) == ('lost', 2)

    # Incomplete match is pending
    m_live = SimpleNamespace(status='live', ht_home=1, ht_away=0, ft_home=1, ft_away=0)
    assert settle(m_live, None, {'selection': 'over_0_5', 'market': 'first_half_goals'}) == ('pending', None)

    # Missing scores are awaiting data
    m_missing = SimpleNamespace(status='finished', ht_home=None, ht_away=0, ft_home=2, ft_away=1)
    assert settle(m_missing, None, {'selection': 'over_0_5', 'market': 'first_half_goals'}) == ('awaiting_data', None)

    # Impossible combinations void
    m_bad = SimpleNamespace(status='finished', ht_home=3, ht_away=0, ft_home=2, ft_away=0)
    assert settle(m_bad, None, {'selection': 'over_0_5', 'market': 'second_half_goals'}) == ('void', None)

    # Cancelled/abandoned void
    m_canc = SimpleNamespace(status='cancelled', ht_home=1, ht_away=0, ft_home=1, ft_away=0)
    assert settle(m_canc, None, {'selection': 'over_0_5', 'market': 'first_half_goals'}) == ('void', None)


def test_half_goal_predictions_quality_gates_and_candidate_groups():
    # Build 80 synthetic matches with consistent halftime/fulltime scores
    matches = []
    for i in range(80):
        matches.append(SimpleNamespace(
            id=i+1,
            league_id=1,
            season_id=1,
            home_team_id=1 if i % 2 == 0 else 2,
            away_team_id=2 if i % 2 == 0 else 1,
            status='finished',
            ht_home=1,
            ht_away=1,
            ft_home=2,
            ft_away=2,
            kickoff_at=NOW - timedelta(days=i+1)
        ))

    # Prediction ok with sufficient sample
    pred_first = half_goal_prediction(matches, 1, 2, 'first')
    assert pred_first['status'] == 'ok'
    assert pred_first['expected_total'] is not None
    assert '0.5' in pred_first['over_probabilities']
    assert '1.5' in pred_first['over_probabilities']
    assert '2.5' in pred_first['over_probabilities']

    # Insufficient sample
    pred_small = half_goal_prediction(matches[:10], 1, 2, 'first')
    assert pred_small['status'] == 'insufficient_data'
    assert any(r['code'] == 'league_sample_size' for r in pred_small['insufficient_reasons'])

    # Build recommendations and verify candidate correlation groups
    profile_first = {k: half_goal_summary(matches[:n], 1, 'first') for k, n in [('last_5', 5), ('last_10', 10), ('season', 80)]}
    profile_second = {k: half_goal_summary(matches[:n], 1, 'second') for k, n in [('last_5', 5), ('last_10', 10), ('season', 80)]}
    mock_result = {
        'prediction': {'status': 'insufficient_data'},
        'match': {'competition_type': 'club'},
        'additional_statistics': {
            'first_half_goals': {'home': profile_first, 'away': profile_first, 'prediction': pred_first},
            'second_half_goals': {'home': profile_second, 'away': profile_second, 'prediction': half_goal_prediction(matches, 1, 2, 'second')}
        }
    }
    candidates = build_candidates(mock_result)
    assert any(c['market'] == 'first_half_goals' and c['correlation_group'] == 'first_half_goals' for c in candidates)
    assert any(c['market'] == 'second_half_goals' and c['correlation_group'] == 'second_half_goals' for c in candidates)

    ranked, selected = select_recommendations(candidates)
    # Selected recommendations must respect correlation group isolation (at most 1 per group)
    assert sum(c['correlation_group'] == 'first_half_goals' for c in selected) <= 1
    assert sum(c['correlation_group'] == 'second_half_goals' for c in selected) <= 1
    assert len(selected) <= 3


async def seed_half_goals(db, n=80, refresh=True):
    from sqlalchemy import update
    import src.analytics.service as service
    target, league, season, teams, history = await seed(db)
    service._cache.clear()
    async with db.session() as session:
        await session.execute(
            update(Match)
            .where(Match.id.in_(history[:n]))
            .values(ht_home=1, ht_away=1, ft_home=2, ft_away=2, updated_at=NOW - timedelta(days=1))
        )
        await session.commit()
    service._cache.clear()
    if refresh:
        from src.analytics.board_cache import refresh_one
        await refresh_one(db, now=NOW, force_league=league)
    return target, league, history


async def verify_half_goal_persistence(db):
    target, league, history = await seed_half_goals(db, refresh=False)
    async with db.session() as session:
        from src.api.routes import match_query, match_response
        query, _, _ = match_query()
        row = (await session.execute(query.where(Match.id == target))).first()
        result = await statistics(session, row[0], match_response(row), NOW)
        extra = result['additional_statistics']

        assert 'first_half_goals' in extra
        assert 'second_half_goals' in extra
        assert extra['first_half_goals']['prediction']['status'] == 'ok'
        assert extra['second_half_goals']['prediction']['status'] == 'ok'

        # Home and away splits verified
        for side in ('home', 'away'):
            assert extra['first_half_goals'][side]['last_5']['for_avg'] is not None
            assert extra['first_half_goals'][side]['last_10']['total_avg'] == 2.0
            assert extra['second_half_goals'][side]['last_10']['total_avg'] == 2.0

        # Durable board cache refresh
        from src.analytics.board_cache import refresh_one, read_board, MODEL_VERSION
        await refresh_one(db, now=NOW, force_league=league)
        items, gen, evaluated = await read_board(session, str(league), None, None, NOW)
        assert evaluated > 0
        board = await session.get(PredictionBoard, league)
        assert board is not None
        assert board.model_version == MODEL_VERSION
        assert 'items' in board.payload


async def test_sqlite_half_goals_and_durable_board_cache(db):
    await verify_half_goal_persistence(db)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_half_goals_and_durable_board_cache():
    db = Database(os.environ['TEST_POSTGRES_URL'])
    schema = 'half_goals_' + uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        db.engine = db.engine.execution_options(schema_translate_map={None: schema})
        db.sessions = async_sessionmaker(db.engine, expire_on_commit=False)
        await db.initialize()
        await verify_half_goal_persistence(db)
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await db.close()


async def test_api_half_goal_market_filters(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    target, league, _ = await seed_half_goals(db)

    # Match statistics endpoint includes first_half_goals and second_half_goals
    data = (await api.get(f'/api/matches/{target}/statistics')).json()
    assert data['prediction']['first_half_goals_status'] == 'ok'
    assert data['prediction']['second_half_goals_status'] == 'ok'
    assert 'first_half_goals' in data['additional_statistics']
    assert 'second_half_goals' in data['additional_statistics']

    # Prediction endpoints with market filters
    for market in ('first_half_goals', 'second_half_goals'):
        res = await api.get(f'/api/predictions?market={market}')
        assert res.status_code == 200
        best_res = await api.get(f'/api/predictions/best?market={market}')
        assert best_res.status_code == 200
        perf_res = await api.get(f'/api/market-performance?market={market}')
        assert perf_res.status_code == 200


async def test_chromium_half_goals_desktop_and_mobile_ui(api, db, monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    target, _, _ = await seed_half_goals(db)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))

        async def forward(route):
            response = await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code, body=response.content, content_type=response.headers.get('content-type', 'application/json'))

        await page.route('http://test/**', forward)
        await page.goto('http://test/')

        # Open match detail dialog
        await page.evaluate('(id)=>BetAppDashboard.selectMatch(id,true)', target)

        # First half goals tab
        await page.locator('[data-analysis-tab=first_half_goals]').click()
        await expect(page.locator('#analysis-panel')).to_contain_text('1. YARI GOLLER')
        await expect(page.locator('#analysis-panel')).to_contain_text('Geçmiş takım istatistikleri')
        await expect(page.locator('#analysis-panel')).to_contain_text('1. Yarı toplam gol ortalaması')

        # Second half goals tab
        await page.locator('[data-analysis-tab=second_half_goals]').click()
        await expect(page.locator('#analysis-panel')).to_contain_text('2. YARI GOLLER')
        await expect(page.locator('#analysis-panel')).to_contain_text('2. Yarı toplam gol ortalaması')

        # Mobile viewport responsiveness
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.locator('[data-analysis-tab=first_half_goals]').click()
        await expect(page.locator('#analysis-panel')).to_contain_text('1. YARI GOLLER')
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert await page.locator('#analysis-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')

        assert errors == []
        await browser.close()
