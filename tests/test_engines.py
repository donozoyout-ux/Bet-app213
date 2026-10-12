from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.analytics.engines import (PrematchContext, EngineRegistry, LegacyAdapter,
                                  evaluate_binary, attach_coverage, historical_backtest)
from src.analytics.predictions import Result, poisson_probabilities
from src.analytics.odds_matching import price_observed_at, price_matches_line
from .test_predictions import NOW


def context():
    history = [Result(i, 1 if i % 2 else 2, 2 if i % 2 else 1,
                      i % 3 + 1, i % 2, NOW-timedelta(days=i+2)) for i in range(80)]
    return PrematchContext.load(999, NOW+timedelta(days=1), NOW, history, 1, 2, 'club')[0]


def test_four_adapters_match_shared_distribution():
    ctx = context()
    reports = EngineRegistry().run(ctx)
    assert len(reports) == 4
    assert all(r['status'] == 'available' for r in reports.values())
    base = poisson_probabilities(ctx.prediction['expected_home_goals'], ctx.prediction['expected_away_goals'])
    result = reports['result_1x2']['probabilities']
    assert sum(result.values()) == pytest.approx(1)
    assert reports['double_chance']['probabilities']['1x'] == pytest.approx(result['home']+result['draw'])
    assert reports['both_teams_to_score']['probabilities']['yes'] == base['btts_probability']
    assert reports['total_goals']['probabilities']['over_2_5'] == base['over_25_probability']
    assert reports['result_1x2']['calibration']['status'] == 'unvalidated'


def test_failure_and_disable_are_isolated():
    class Broken(LegacyAdapter):
        market_id = 'broken'
        def predict(self, context):
            raise RuntimeError('test failure')
    registry = EngineRegistry(disabled=['total_goals'])
    registry.engines['broken'] = Broken()
    reports = registry.run(context())
    assert reports['broken']['missing_reason'] == 'engine_error:RuntimeError'
    assert reports['total_goals']['missing_reason'] == 'engine_disabled'
    assert reports['result_1x2']['status'] == 'available'


def test_missing_inputs_and_leakage():
    ctx = context()
    future = Result(888, 1, 2, 99, 99, NOW+timedelta(days=2))
    target = Result(999, 1, 2, 99, 99, NOW-timedelta(days=5))
    new, _ = PrematchContext.load(999, ctx.kickoff_at, NOW, [*ctx.history, future, target], 1, 2, 'club')
    assert new.prediction == ctx.prediction
    empty, _ = PrematchContext.load(999, ctx.kickoff_at, NOW, [], 1, 2, 'club')
    assert all(r['missing_reason']=='insufficient_prematch_data' for r in EngineRegistry().run(empty).values())
    with pytest.raises(ValueError):
        PrematchContext.load(999, NOW.replace(tzinfo=None), NOW, [], 1, 2, 'club')


def test_field_metadata_does_not_borrow_row_timestamp_or_line():
    row = SimpleNamespace(updated_at=NOW, latest_line=2.5,
                          raw={'_field_observed_at': {}, '_field_lines': {}})
    assert price_observed_at(row, 'latest_over') is None
    assert not price_matches_line(row, 'latest', 'over', 2.5)
    row.raw = {'_field_observed_at': {'latest_over': 'invalid'}}
    assert price_observed_at(row, 'latest_over') is None
    row.raw = {}
    assert price_observed_at(row, 'latest_over') == NOW


def test_brier_and_calibration_require_actual_outcomes():
    assert evaluate_binary([])['brier'] is None
    result = evaluate_binary([(.8, 1), (.2, 0), (1, 1)])
    assert result['brier'] == pytest.approx(.08/3)
    assert sum(b['count'] for b in result['calibration']) == 3
    with pytest.raises(ValueError):
        evaluate_binary([(float('nan'), 1)])


def test_every_market_has_coverage_even_when_no_prices():
    reports = EngineRegistry().run(context())
    attach_coverage(reports, [])
    assert all(r['coverage']['priced']==0 for r in reports.values())


def test_historical_backtest_measures_each_market_without_future_history():
    from dataclasses import replace
    ctx = context()
    outcome = Result(ctx.match_id, 1, 2, 2, 1, ctx.kickoff_at)
    metrics = historical_backtest([(ctx, outcome)])
    assert len(metrics) == 4
    assert all(values and all(v['sample_size']==1 for v in values.values())
               for values in metrics.values())
    with pytest.raises(ValueError):
        historical_backtest([(replace(ctx, history=(*ctx.history, outcome)), outcome)])


async def test_statistics_exposes_engines_and_disabled_candidates(api, db, monkeypatch):
    from .test_predictions import seed
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    match_id, *_ = await seed(db)
    response = await api.get(f'/api/matches/{match_id}/statistics')
    assert response.status_code == 200
    data = response.json()
    assert len(data['market_engines']) == 4
    assert data['engine_context']['match_id'] == match_id
    monkeypatch.setenv('DISABLED_MARKET_ENGINES', 'double_chance')
    disabled = (await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert disabled['market_engines']['double_chance']['missing_reason']=='engine_disabled'
    assert not any(c['market']=='double_chance' for c in disabled['candidates'])
    assert any(c['market']=='result' for c in disabled['candidates'])
