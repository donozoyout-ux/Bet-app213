"""Synthetic evidence only. Contract tests never open external connections."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from sqlalchemy import event

from src.analytics.coupon_editions import (
    Edition, KINDS, canonical, project, project_four, propose, read_saved,
    telegram_dry_run,
)
from src.analytics.coupon_products import compose_products
from src.analytics.performance import settle
from src.models import Match, SavedCoupon
from tests.test_dynamic_coupon import entry
from tests.test_predictions import NOW

DAY = NOW.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Istanbul')).date().isoformat()


def products():
    records = [entry(i, p=.95, price=p)
        for i, p in enumerate((1.85, 1.85, 1.85, 2., 2., 2.1), 1)]
    for record in records:
        record['quote'].update(period='full_time', line=None)
    return compose_products(records, NOW)


def freeze(kind='banko', product=None, **kwargs):
    return propose(product or products()[kind], day=DAY, now=NOW,
                   model_version='synthetic-v1', **kwargs)


@pytest.mark.parametrize('kind', KINDS)
def test_publication_home_coupon_telegram_audit_settlement_same_evidence(kind):
    original = freeze(kind)
    stored = Edition(original.document)  # restart/serialization round trip
    home = project_four({kind: stored}, DAY, NOW)[kind]
    coupon = project(stored, NOW)
    audit = project(stored, NOW)
    telegram = telegram_dry_run(stored, NOW)
    for view in (home, coupon, audit, telegram['edition'], json.loads(telegram['text'])):
        for key in ('day', 'kind', 'edition_id', 'content_digest', 'items'):
            assert view[key] == original.as_dict()[key]
    for item in audit['items']:
        match = Match(id=item['match']['id'], status='finished', ft_home=2, ft_away=0)
        assert settle(match, None, item['recommendation']) == ('won', '2-0')
        match.ft_home = None
        assert settle(match, None, item['recommendation'])[0] == 'awaiting_data'
    assert original.document == stored.document


def test_retry_restart_does_not_reassess_first_frozen_record():
    product = products()['banko']
    first = freeze(product=product)
    later = NOW + timedelta(minutes=20)
    retry = propose(product, day=DAY, now=later, model_version='synthetic-v1',
                    previous=Edition(first.document))
    assert retry.document == first.document
    stale = project(retry, later)
    assert stale['status'] == 'stale' and stale['first_record']
    assert stale['items'] == first.as_dict()['items']
    assert not telegram_dry_run(retry, later)['sendable']


def test_explicit_revision_includes_diff_preserves_first_and_requires_scope():
    first = freeze()
    changed = deepcopy(products()['banko'])
    changed['items'][0]['quote']['price'] = 1.80
    with pytest.raises(ValueError, match='explicit_revision'):
        freeze(product=changed, previous=first)
    second = freeze(product=changed, previous=first, new_revision=True)
    a, b = first.as_dict(), second.as_dict()
    assert b['revision'] == 2 and b['previous_id'] == a['edition_id']
    assert b['first_edition_id'] == a['edition_id'] and b['diff']['items']
    assert a['items'][0]['quote']['price'] == 1.85
    assert a['content_digest'] != b['content_digest']
    with pytest.raises(ValueError, match='revision_scope'):
        freeze('balanced', previous=first, new_revision=True)


def test_content_integrity_and_detached_projection():
    first = freeze()
    data = first.as_dict()
    data['items'][0]['quote']['price'] = 9
    with pytest.raises(ValueError, match='digest'):
        Edition(canonical(data))
    view = project(first, NOW)
    view['items'].clear()
    assert first.as_dict()['items']
    assert freeze().document == first.document


@pytest.mark.parametrize('field,value', [('price', 0), ('price', True),
    ('price', float('nan')), ('bookmaker', 'Fake'), ('eligible', False),
    ('observed_at', (NOW-timedelta(seconds=901)).isoformat()),
    ('observed_at', (NOW+timedelta(seconds=1)).isoformat()),
    ('expires_at', NOW.isoformat())])
def test_invalid_quote_never_published(field, value):
    product = deepcopy(products()['banko'])
    product['items'][0]['quote'][field] = value
    with pytest.raises(ValueError):
        freeze(product=product)


@pytest.mark.parametrize('field,value', [('probability', .699), ('reliability', .799), ('score', .799)])
def test_quality_floors(field, value):
    product = deepcopy(products()['banko'])
    product['items'][0]['recommendation'][field] = value
    with pytest.raises(ValueError, match='unqualified'):
        freeze(product=product)


def test_same_match_conflicts_and_line_mismatch():
    product = deepcopy(products()['balanced'])
    product['items'].append(deepcopy(product['items'][0]))
    product['items'][-1]['recommendation'].update(id='away_result', selection='away')
    with pytest.raises(ValueError, match='unqualified'):
        freeze('balanced', product=product)
    product = deepcopy(products()['banko'])
    product['items'][0]['recommendation']['line'] = 2.5
    product['items'][0]['quote']['line'] = 3.5
    with pytest.raises(ValueError, match='mismatch'):
        freeze(product=product)


def test_absent_no_eligible_and_incomplete_are_distinct():
    assert all(v['status'] == 'absent' for v in project_four({}, DAY, NOW).values())
    empty = compose_products([], NOW)['banko']
    assert project(freeze(product=empty), NOW)['status'] == 'no_eligible'
    empty['reason'] = 'search_budget_exhausted'
    assert project(freeze(product=empty), NOW)['status'] == 'incomplete'
    assert not project(freeze(product=empty), NOW)['published']


def test_day_kind_namespaces_istanbul_boundary_and_aware_timestamps():
    editions = [freeze(kind).as_dict()['edition_id'] for kind in KINDS]
    assert len(set(editions)) == 4
    empty = compose_products([], NOW)['banko']
    tomorrow = NOW + timedelta(days=1)
    other = propose(empty, day=tomorrow.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Istanbul')).date().isoformat(),
                    now=tomorrow, model_version='synthetic-v1')
    assert other.as_dict()['edition_id'] != freeze(product=empty).as_dict()['edition_id']
    from datetime import datetime
    midnight = datetime.fromisoformat('2026-10-30T21:00:00+00:00')
    assert propose(empty, day='2026-10-31', now=midnight, model_version='v1')
    with pytest.raises(ValueError, match='outside_publication_day'):
        propose(empty, day='2026-10-30', now=midnight, model_version='v1')
    with pytest.raises(ValueError, match='timezone_aware'):
        project(freeze(), NOW.replace(tzinfo=None))


def test_observation_changes_are_content_changes_and_live_updates_do_not_leak():
    product = deepcopy(products()['banko'])
    first = freeze(product=product)
    product['items'][0]['quote']['observed_at'] = (NOW-timedelta(seconds=1)).isoformat()
    second = freeze(product=product, previous=first, new_revision=True)
    assert second.as_dict()['content_digest'] != first.as_dict()['content_digest']
    product['items'][0]['recommendation']['selection'] = 'away'
    assert project(first, NOW)['items'][0]['recommendation']['selection'] == 'home'


def test_missing_ht_does_not_count_as_loss():
    match = Match(status='finished', ft_home=2, ft_away=0, ht_home=None, ht_away=None)
    for market in ('first_half_goals', 'second_half_goals'):
        assert settle(match, None, dict(market=market, selection='over_0_5'))[0] == 'awaiting_data'


async def test_legacy_adapter_one_select_no_autoflush_or_mutation(db):
    legacy = {'items': [{'match': {'id': 1982}, 'quote': {'price': 1.8}}]}
    async with db.session() as session:
        session.add(SavedCoupon(id='legacy', root_id='legacy', revision=1,
            kind='automatic', owner_hash=None, created_at=NOW, payload=legacy))
        session.add(SavedCoupon(id='private', root_id='private', revision=1,
            kind='manual', owner_hash='secret', created_at=NOW, payload=legacy))
        await session.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.engine.sync_engine, 'before_cursor_execute', capture)
    try:
        async with db.session() as session:
            session.add(SavedCoupon(id='pending', root_id='pending', revision=1,
                kind='automatic', created_at=NOW, payload=legacy))
            result = await read_saved(session, 'legacy')
            assert len(statements) == 1 and statements[0].lstrip().upper().startswith('SELECT')
            assert result['source_id'] == 'legacy' and result['first_record']
            assert result['evidence_status'] == 'legacy_unverified'
            assert result['legacy_payload'] == legacy
            assert 'edition_id' not in result
            result['legacy_payload']['items'].clear()
            assert (await read_saved(session, 'legacy'))['legacy_payload'] == legacy
            assert await read_saved(session, 'private') is None
            assert await read_saved(session, 'missing') is None
            assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
    finally:
        event.remove(db.engine.sync_engine, 'before_cursor_execute', capture)


@pytest.mark.parametrize('change', ['period', 'line_missing', 'quote_match', 'selection_id'])
def test_quote_binding_is_required(change):
    product = deepcopy(products()['banko'])
    item = product['items'][0]
    if change == 'period':
        item['quote']['period'] = 'first_half'
    elif change == 'line_missing':
        del item['quote']['line']
    elif change == 'quote_match':
        item['quote']['match_id'] = 9999
    else:
        item['recommendation']['selection'] = 'away'
    with pytest.raises(ValueError, match='mismatch'):
        freeze(product=product)


def test_supplied_total_is_not_trusted():
    product = deepcopy(products()['banko'])
    product['combined_odds'] = 900
    assert freeze(product=product).as_dict()['combined_odds'] == '1.85'


def test_invalid_targets_and_backdated_revision_rejected():
    product = deepcopy(products()['banko'])
    product['items'][0]['quote']['price'] = 2.5
    with pytest.raises(ValueError, match='invalid_banko'):
        freeze(product=product)
    product['kind'] = 'combined_6_7'
    with pytest.raises(ValueError, match='invalid_target_range'):
        freeze(product=product)
    with pytest.raises(ValueError, match='revision_before_previous'):
        propose(products()['banko'], day=DAY, now=NOW-timedelta(seconds=1),
                model_version='synthetic-v1', previous=freeze(), new_revision=True)
