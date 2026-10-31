"""Four coupon types are computed only from proven, fresh synthetic candidates."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from src.analytics.coupon_products import compose_products, BANKO_MIN, BANKO_MAX
from tests.test_dynamic_coupon import entry
from tests.test_predictions import NOW


def test_four_independent_products_have_true_price_targets_without_publication():
    records = [entry(i, p=.95, quality=.98, price=price)
               for i, price in enumerate((1.85, 1.85, 1.85, 2.0, 2.0, 2.1), 1)]
    original = deepcopy(records)
    response = compose_products(records, NOW)
    assert set(response) == {
        'policy_version', 'banko', 'balanced', 'combined_6_7',
        'combined_8_10', 'balanced_audit', 'publication_note'
    }
    for key in ('banko','balanced','combined_6_7','combined_8_10'):
        product = response[key]
        assert product['published'] is False
        assert product['status'] == 'candidate_only'
        assert product['items']
        assert len({p['match']['id'] for p in product['items']}) == len(product['items'])
        value = Decimal(1)
        for pick in product['items']:
            assert pick['quote']['bookmaker'] in ('Crown','Bet365','Sbobet')
            assert pick['quote']['eligible']
            assert pick['recommendation']['probability'] >= .70
            assert pick['recommendation']['reliability'] >= .80
            assert pick['recommendation']['score'] >= .80
            value *= Decimal(str(pick['quote']['price']))
        assert product['combined_odds'] == float(value)
    assert len(response['banko']['items']) == 1
    assert BANKO_MIN <= Decimal(str(response['banko']['combined_odds'])) <= BANKO_MAX
    assert 6 <= response['combined_6_7']['combined_odds'] <= 7
    assert 8 <= response['combined_8_10']['combined_odds'] <= 10
    assert records == original  # No mutation of source candidates or frozen history.


def test_missing_target_never_invents_prices_or_forces_a_coupon():
    records = [entry(1, p=.95, quality=.98, price=1.8)]
    products = compose_products(records, NOW)
    assert products['banko']['status']=='candidate_only'
    assert products['balanced']['status']=='candidate_only'
    for key in ('combined_6_7','combined_8_10'):
        assert products[key]['status']=='unavailable'
        assert products[key]['items']==[] and products[key]['combined_odds'] is None
        assert products[key]['reason']=='no_target_range_from_qualified_portfolio'


def test_stale_or_fake_odds_and_low_quality_cannot_enter_any_coupon():
    invalid=[entry(1,p=.699,price=1.8),entry(2,p=.95,price=1.8),
             entry(3,p=.95,price=2.0)]
    invalid[1]['quote']['observed_at']=(NOW-timedelta(seconds=901)).isoformat()
    invalid[2]['quote']['bookmaker']='UnknownProvider'
    result=compose_products(invalid,NOW)
    for kind in ('banko','balanced','combined_6_7','combined_8_10'):
        assert result[kind]['items']==[]
        assert result[kind]['published'] is False


def test_deterministic_selection_and_independent_matches():
    records=[entry(i,p=.95,price=1.85) for i in range(1,9)]
    first=compose_products(records,NOW)
    reverse=compose_products(list(reversed(records)),NOW)
    for name in ('banko','balanced','combined_6_7','combined_8_10'):
        a,b=first[name],reverse[name]
        assert a['combined_odds']==b['combined_odds']
        assert [i['match']['id'] for i in a['items']]==[i['match']['id'] for i in b['items']]
    assert compose_products([],NOW)['balanced']['status']=='unavailable'


def test_evaluation_cap_reports_unexamined_portfolios_not_fake_unavailability(monkeypatch):
    import src.analytics.coupon_products as products
    monkeypatch.setattr(products, 'COMBINATION_SEARCH_MAX', 2)
    source=[entry(i,p=.95,price=1.85) for i in range(1,7)]
    results=compose_products(source,NOW)
    assert len(results['balanced']['items']) > 2
    for kind in ('combined_6_7','combined_8_10'):
        assert results[kind]['status']=='unavailable'
        assert results[kind]['reason']=='search_budget_exhausted'
