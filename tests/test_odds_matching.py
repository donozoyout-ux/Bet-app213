from datetime import datetime, timezone
import pytest
from src.analytics.odds_matching import (
    parse_market_and_selection,
    match_odds_for_candidate,
    enrich_recommendations_with_odds,
    NO_ODDS_MESSAGE,
    SUPPORTED_BOOKMAKERS,
)


class MockOdds1X2:
    def __init__(self, op_h, op_d, op_a, lat_h, lat_d, lat_a, cl_h=None, cl_d=None, cl_a=None, updated_at=None):
        self.opening_home = op_h
        self.opening_draw = op_d
        self.opening_away = op_a
        self.latest_home = lat_h
        self.latest_draw = lat_d
        self.latest_away = lat_a
        self.closing_home = cl_h
        self.closing_draw = cl_d
        self.closing_away = cl_a
        self.updated_at = updated_at or datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


class MockAsianTotals:
    def __init__(self, op_line, op_o, op_u, lat_line, lat_o, lat_u, cl_line=None, cl_o=None, cl_u=None, updated_at=None):
        self.opening_line = op_line
        self.opening_over = op_o
        self.opening_under = op_u
        self.latest_line = lat_line
        self.latest_over = lat_o
        self.latest_under = lat_u
        self.closing_line = cl_line
        self.closing_over = cl_o
        self.closing_under = cl_u
        self.updated_at = updated_at or datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


class MockAsianHandicap:
    def __init__(self, op_line, op_h, op_a, lat_line, lat_h, lat_a, cl_line=None, cl_h=None, cl_a=None, updated_at=None):
        self.opening_line = op_line
        self.opening_home = op_h
        self.opening_away = op_a
        self.latest_line = lat_line
        self.latest_home = lat_h
        self.latest_away = lat_a
        self.closing_line = cl_line
        self.closing_home = cl_h
        self.closing_away = cl_a
        self.updated_at = updated_at or datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def test_parse_market_and_selection():
    assert parse_market_and_selection('result', 'home') == ('result', 'home', None, 'full_time')
    assert parse_market_and_selection('result', 'draw') == ('result', 'draw', None, 'full_time')
    assert parse_market_and_selection('result', 'away') == ('result', 'away', None, 'full_time')
    assert parse_market_and_selection('goals', 'over_2_5') == ('goals', 'over', 2.5, 'full_time')
    assert parse_market_and_selection('goals', 'under_1_5') == ('goals', 'under', 1.5, 'full_time')
    assert parse_market_and_selection('asian_handicap', 'home_minus_0_5') == ('asian_handicap', 'home', -0.5, 'full_time')
    assert parse_market_and_selection('asian_handicap', 'away_plus_0_5') == ('asian_handicap', 'away', -0.5, 'full_time')
    assert parse_market_and_selection('first_half_goals', 'over_1_5') == ('first_half_goals', 'over', 1.5, 'first_half')
    assert parse_market_and_selection('second_half_goals', 'over_0_5') == ('second_half_goals', 'over', 0.5, 'second_half')


def test_exact_1x2_matching():
    candidate = {'market': 'result', 'selection': 'home', 'probability': 0.60}
    odds_1x2 = [
        (MockOdds1X2(1.80, 3.50, 4.20, 1.85, 3.60, 4.40), 'Crown'),
        (MockOdds1X2(1.75, 3.40, 4.10, 1.90, 3.50, 4.00), 'Bet365'),
        (MockOdds1X2(1.82, 3.55, 4.15, 1.82, 3.55, 4.15), 'Sbobet'),
    ]
    res = match_odds_for_candidate(candidate, match_id=1, match_status='scheduled', odds_1x2_rows=odds_1x2)
    assert res['has_odds'] is True
    assert res['period'] == 'full_time'
    assert res['stage'] == 'latest'
    assert res['best_price'] == 1.90
    assert res['best_bookmaker'] == 'Bet365'
    assert res['best_implied_probability'] == round(1.0 / 1.90, 4)
    # value = 0.60 * 1.90 - 1 = 1.14 - 1 = 0.14
    assert res['best_value'] == 0.14
    assert res['message'] is None

    # Check each bookmaker
    assert res['bookmakers']['Crown']['opening_odds'] == 1.80
    assert res['bookmakers']['Crown']['current_odds'] == 1.85
    assert res['bookmakers']['Bet365']['current_odds'] == 1.90
    assert res['bookmakers']['Sbobet']['current_odds'] == 1.82


def test_exact_goals_matching_and_line_rejection():
    candidate_25 = {'market': 'goals', 'selection': 'over_2_5', 'probability': 0.55}
    # Bet365 has line 2.5, Crown has line 2.75 (does NOT match), Sbobet has line 2.5
    asian_totals = [
        (MockAsianTotals(2.5, 1.95, 1.85, 2.75, 2.10, 1.70), 'Crown'),  # latest line moved to 2.75!
        (MockAsianTotals(2.5, 1.90, 1.90, 2.5, 1.92, 1.88), 'Bet365'),
        (MockAsianTotals(2.5, 1.88, 1.92, 2.5, 1.95, 1.85), 'Sbobet'),
    ]
    res = match_odds_for_candidate(candidate_25, match_id=1, match_status='scheduled', asian_totals_rows=asian_totals)
    assert res['has_odds'] is True
    # Crown's latest line is 2.75, which does not match 2.5! But opening was 2.5
    assert res['bookmakers']['Crown']['latest_odds'] is None
    assert res['bookmakers']['Crown']['opening_odds'] == 1.95
    assert res['bookmakers']['Crown']['current_odds'] == 1.95  # falls back to opening since latest line differs

    # Best price between Bet365 (1.92), Sbobet (1.95), Crown (1.95)
    assert res['best_price'] == 1.95
    assert res['message'] is None


def test_first_half_never_substitutes_full_time():
    candidate_1h = {'market': 'first_half_goals', 'selection': 'over_1_5', 'probability': 0.70}
    asian_totals = [
        (MockAsianTotals(1.5, 1.30, 3.20, 1.5, 1.35, 3.00), 'Bet365'),
    ]
    # Passing full-time Asian totals must NOT be used for first-half goals
    res = match_odds_for_candidate(candidate_1h, match_id=1, match_status='scheduled', asian_totals_rows=asian_totals)
    assert res['has_odds'] is False
    assert res['period'] == 'first_half'
    assert res['best_price'] is None
    assert res['message'] == NO_ODDS_MESSAGE


def test_second_half_never_substitutes_full_time():
    candidate_2h = {'market': 'second_half_goals', 'selection': 'over_0_5', 'probability': 0.80}
    asian_totals = [
        (MockAsianTotals(0.5, 1.10, 6.0, 0.5, 1.12, 5.5), 'Crown'),
    ]
    res = match_odds_for_candidate(candidate_2h, match_id=1, match_status='scheduled', asian_totals_rows=asian_totals)
    assert res['has_odds'] is False
    assert res['period'] == 'second_half'
    assert res['best_price'] is None
    assert res['message'] == NO_ODDS_MESSAGE


def test_unsupported_markets_no_invented_odds():
    for market, selection in [('btts', 'yes'), ('double_chance', '1x'), ('corners', 'over_9_5'), ('cards', 'over_3_5')]:
        c = {'market': market, 'selection': selection, 'probability': 0.65}
        res = match_odds_for_candidate(c, match_id=1, match_status='scheduled')
        assert res['has_odds'] is False
        assert res['best_price'] is None
        assert res['message'] == NO_ODDS_MESSAGE


def test_closing_stage_for_finished_matches():
    candidate = {'market': 'result', 'selection': 'away', 'probability': 0.40}
    odds_1x2 = [
        (MockOdds1X2(3.0, 3.2, 2.3, 3.1, 3.3, 2.2, cl_h=3.2, cl_d=3.4, cl_a=2.1), 'Bet365'),
    ]
    res_sched = match_odds_for_candidate(candidate, match_id=1, match_status='scheduled', odds_1x2_rows=odds_1x2)
    assert res_sched['stage'] == 'latest'
    assert res_sched['best_price'] == 2.2

    res_fin = match_odds_for_candidate(candidate, match_id=1, match_status='finished', odds_1x2_rows=odds_1x2)
    assert res_fin['stage'] == 'closing'
    assert res_fin['best_price'] == 2.1


def test_stale_prices_cutoff():
    candidate = {'market': 'result', 'selection': 'home', 'probability': 0.50}
    stale_time = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    cutoff = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc)
    odds_1x2 = [
        (MockOdds1X2(1.8, 3.5, 4.0, 1.9, 3.5, 4.0, updated_at=stale_time), 'Crown'),
    ]
    res = match_odds_for_candidate(candidate, match_id=1, match_status='scheduled', odds_1x2_rows=odds_1x2, cutoff=cutoff)
    assert res['has_odds'] is False
    assert res['message'] == NO_ODDS_MESSAGE


def test_enrich_recommendations():
    candidates = [
        {'id': 'home_result', 'market': 'result', 'selection': 'home', 'probability': 0.60, 'label': 'Ev Sahibi'},
        {'id': 'over_2_5_goals', 'market': 'goals', 'selection': 'over_2_5', 'probability': 0.65, 'label': '2.5 Gol Üst'},
        {'id': 'over_1_5_first_half_goals', 'market': 'first_half_goals', 'selection': 'over_1_5', 'probability': 0.70, 'label': '1. Yarı 1.5 Üst'},
    ]
    odds_1x2 = [(MockOdds1X2(2.0, 3.2, 3.8, 2.1, 3.2, 3.6), 'Crown')]
    totals = [(MockAsianTotals(2.5, 1.85, 1.95, 2.5, 1.90, 1.90), 'Bet365')]

    enrich_recommendations_with_odds(candidates, match_id=1, match_status='scheduled', odds_1x2_rows=odds_1x2, asian_totals_rows=totals)

    assert candidates[0]['verified_odds']['has_odds'] is True
    assert candidates[0]['verified_odds']['best_price'] == 2.1

    assert candidates[1]['verified_odds']['has_odds'] is True
    assert candidates[1]['verified_odds']['best_price'] == 1.90

    assert candidates[2]['verified_odds']['has_odds'] is False
    assert candidates[2]['verified_odds']['message'] == NO_ODDS_MESSAGE


def test_corners_and_cards_odds_matching():
    candidate_corner = {'market': 'corners', 'selection': 'over_9_5', 'probability': 0.58}
    corner_rows = [
        (MockAsianTotals(9.5, 1.85, 1.95, 9.5, 1.92, 1.88), 'Bet365'),
        (MockAsianTotals(9.5, 1.80, 2.00, 10.5, 2.10, 1.75), 'Crown'),
    ]
    res = match_odds_for_candidate(candidate_corner, match_id=1, match_status='scheduled', period_odds_rows=corner_rows)
    assert res['has_odds'] is True
    assert res['best_price'] == 1.92
    assert res['best_bookmaker'] == 'Bet365'
    assert res['bookmakers']['Crown']['current_odds'] == 1.80  # fallback to opening line 9.5 since latest line is 10.5

    # Line mismatch
    candidate_mismatch = {'market': 'corners', 'selection': 'over_11_5', 'probability': 0.40}
    res_mis = match_odds_for_candidate(candidate_mismatch, match_id=1, match_status='scheduled', period_odds_rows=corner_rows)
    assert res_mis['has_odds'] is False
    assert res_mis['best_price'] is None
    assert res_mis['message'] == NO_ODDS_MESSAGE

    # Cards matching
    candidate_cards = {'market': 'cards', 'selection': 'under_3_5', 'probability': 0.62}
    cards_rows = [
        (MockAsianTotals(3.5, 2.10, 1.72, 3.5, 2.05, 1.75), 'Sbobet'),
    ]
    res_card = match_odds_for_candidate(candidate_cards, match_id=1, match_status='scheduled', period_odds_rows=cards_rows)
    assert res_card['has_odds'] is True
    assert res_card['best_price'] == 1.75
    assert res_card['best_bookmaker'] == 'Sbobet'

