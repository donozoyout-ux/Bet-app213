from types import SimpleNamespace
from datetime import datetime, timezone
from src.analytics.odds_matching import match_odds_for_candidate


def test_missing_field_observation_cannot_become_fresh_from_row_update():
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    row = SimpleNamespace(match_id=1, updated_at=now, latest_home=1.8,
                          opening_home=None, closing_home=None,
                          raw={'_field_observed_at': {}})
    quote = match_odds_for_candidate(dict(market='result', selection='home', probability=.8),
                                     1, 'scheduled', [(row, 'Bet365')], cutoff=now)
    assert quote['has_odds']
    assert quote['freshness'] == 'unknown'
    assert not quote['coupon_eligible']
    assert quote['observed_at'] is None
