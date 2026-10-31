"""Only synthetic, localhost betapp_test PostgreSQL; no application migration."""
import asyncio
from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from src.analytics.coupon_editions import Edition, KINDS, canonical, digest, project, propose
from src.analytics.coupon_edition_store import (
    IsolatedEditionStore, MAX_CONTENT_BYTES, PublicationConflict, StoreDisabled,
    content_text, validate_proposal, validate_test_target,
)
from tests.test_coupon_editions import DAY, NOW, freeze, products


def forged(edition, mutate):
    data = edition.as_dict()
    mutate(data)
    content = {k: v for k, v in data.items() if k not in ('edition_id', 'content_digest')}
    checksum = digest(content)
    return Edition(canonical(dict(content, content_digest=checksum, edition_id='ce1:' + checksum)))


def revised(previous, price=1.80, now=NOW):
    product = deepcopy(products()['banko'])
    product['items'][0]['quote']['price'] = price
    return propose(product, day=DAY, now=now, model_version='synthetic-v1',
                   previous=previous, new_revision=True)


def test_store_disabled_before_engine_or_connection():
    with pytest.raises(StoreDisabled):
        IsolatedEditionStore('invalid-url', 'public')


@pytest.mark.parametrize('url,schema', [
    ('postgresql+asyncpg://test@production.example/betapp_test', 'coupon_test_'+'a'*32),
    ('postgresql+asyncpg://test@localhost/production', 'coupon_test_'+'a'*32),
    ('postgresql+asyncpg://test@localhost/betapp_test?host=production.example', 'coupon_test_'+'a'*32),
    ('postgresql+asyncpg://test@localhost/betapp_test', 'public'),
    ('postgresql+asyncpg://test@localhost/betapp_test', 'coupon_test_x;DROP SCHEMA public'),
    ('sqlite+aiosqlite:///:memory:', 'coupon_test_'+'a'*32),
])
def test_remote_or_non_disposable_targets_rejected_without_connection(url, schema):
    with pytest.raises(ValueError, match='isolated_test'):
        validate_test_target(url, schema)


@pytest.mark.parametrize('mutate', [
    lambda d: d['items'][0]['recommendation'].update(probability=.69),
    lambda d: d['items'][0]['recommendation'].update(reliability=.79),
    lambda d: d['items'][0]['recommendation'].update(score=.79),
    lambda d: d['items'][0]['quote'].update(price=9),
    lambda d: d['items'][0]['quote'].update(bookmaker='Fake'),
    lambda d: d['items'][0]['quote'].update(observed_at=(NOW-timedelta(seconds=901)).isoformat()),
    lambda d: d['items'][0]['quote'].update(expires_at=NOW.isoformat()),
    lambda d: d['items'].append(deepcopy(d['items'][0])),
    lambda d: d.update(combined_odds='999'),
    lambda d: d.update(revision=2),
    lambda d: d.update(kind='combined_8_10'),
    lambda d: d.update(day='2026-10-09'),
    lambda d: d.update(first_edition_id='ce1:'+'a'*64),
    lambda d: d.update(diff={'invented': 'change'}),
])
def test_rehashed_bad_contract_is_still_rejected(mutate):
    with pytest.raises(ValueError):
        validate_proposal(forged(freeze(), mutate), None)


def test_utf8_byte_budget_and_normal_four_product_sizes():
    for kind in KINDS:
        assert 0 < len(content_text(freeze(kind)).encode('utf-8')) <= MAX_CONTENT_BYTES
    oversized = forged(freeze(), lambda d: d['items'][0]['match'].update(home_team='ş'*MAX_CONTENT_BYTES))
    with pytest.raises(ValueError, match='byte_budget'):
        content_text(oversized)


def test_no_automatic_schema_or_production_import_registration():
    from src.models import Base
    assert 'coupon_edition' not in Base.metadata.tables
    assert 'coupon_head' not in Base.metadata.tables
    for path in ('src/db.py', 'src/migrations.py', 'src/api/routes.py', 'src/jobs/live.py', 'src/jobs/telegram.py'):
        assert 'coupon_edition_store' not in Path(path).read_text(encoding='utf-8')


@pytest_asyncio.fixture
async def store():
    url = os.getenv('TEST_POSTGRES_URL')
    if not url:
        pytest.skip('isolated TEST_POSTGRES_URL is not configured')
    schema = 'coupon_test_' + uuid4().hex
    instance = IsolatedEditionStore(url, schema, enabled=True)
    # All DDL lives here, after strict target validation. Never call Database.initialize.
    try:
        async with instance.engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            sql = Path('docs/sql/coupon-edition-review.sql').read_text(encoding='utf-8-sig')
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(sql.replace('__SCHEMA__', '"'+schema+'"'))
        yield instance
    finally:
        async with instance.engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await instance.close()


async def counts(store):
    async with store.engine.connect() as conn:
        return tuple([await conn.scalar(text(f'SELECT count(*) FROM {store._table(name)}'))
            for name in ('coupon_edition', 'coupon_head')])


@pytest.mark.parametrize('kind', KINDS)
async def test_postgres_restart_roundtrip_original_identity_and_readonly(store, kind):
    original = freeze(kind)
    await store.append(original, expected_digest=None, now=NOW)
    before = await counts(store)
    # Dispose connections and construct a new store: no process cache or initialization.
    await store.close()
    restarted = IsolatedEditionStore(os.environ['TEST_POSTGRES_URL'], store.schema, enabled=True)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(restarted.engine.sync_engine, 'before_cursor_execute', capture)
    try:
        loaded = await restarted.read(original.as_dict()['edition_id'])
        head = await restarted.head(DAY, kind)
        assert loaded.document == head.document == original.document
        assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
        assert len(statements) == 2
        assert (await restarted.append(original, expected_digest=None, now=NOW+timedelta(days=1))).document == original.document
        assert await counts(restarted) == before == (1, 1)
    finally:
        event.remove(restarted.engine.sync_engine, 'before_cursor_execute', capture)
        await restarted.close()


async def test_postgres_revision_chain_retry_old_version_does_not_rewind(store):
    first = freeze()
    second = revised(first)
    third = revised(second, price=1.75)
    for edition, previous in ((first, None), (second, first), (third, second)):
        await store.append(edition, expected_digest=previous.as_dict()['content_digest'] if previous else None, now=NOW)
    await store.append(second, expected_digest=first.as_dict()['content_digest'], now=NOW+timedelta(days=1))
    assert (await store.head(DAY, 'banko')).document == third.document
    assert (await store.read(first.as_dict()['edition_id'])).document == first.document
    assert await counts(store) == (3, 1)
    changed_quote = deepcopy(products()['banko'])
    changed_quote['items'][0]['quote']['observed_at'] = (NOW-timedelta(seconds=1)).isoformat()
    fourth = propose(changed_quote, day=DAY, now=NOW, model_version='synthetic-v1', previous=third, new_revision=True)
    await store.append(fourth, expected_digest=third.as_dict()['content_digest'], now=NOW)
    assert fourth.as_dict()['content_digest'] != third.as_dict()['content_digest']
    assert await counts(store) == (4, 1)


@pytest.mark.parametrize('initial', [True, False])
@pytest.mark.parametrize('identical', [True, False])
async def test_postgres_two_transactions_cas_race_one_revision(store, monkeypatch, initial, identical):
    first = freeze()
    if not initial:
        await store.append(first, expected_digest=None, now=NOW)
    left = first if initial else revised(first, 1.80)
    right = left if identical else (forged(first, lambda d: d.update(model_version='synthetic-v2')) if initial else revised(first, 1.75))
    expected = None if initial else first.as_dict()['content_digest']
    original_read = store._read
    arrived = 0
    synchronized_tasks = set()
    barrier = asyncio.Event()
    candidate_ids = {x.as_dict()['edition_id'] for x in (left, right)}
    async def simultaneous_read(conn, edition_id):
        nonlocal arrived
        result = await original_read(conn, edition_id)
        task = asyncio.current_task()
        if edition_id in candidate_ids and result is None and task not in synchronized_tasks:
            synchronized_tasks.add(task)
            arrived += 1
            if arrived == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return result
    monkeypatch.setattr(store, '_read', simultaneous_read)
    results = await asyncio.gather(
        store.append(left, expected_digest=expected, now=NOW),
        store.append(right, expected_digest=expected, now=NOW), return_exceptions=True)
    assert arrived == 2  # Both real transactions observed absence before competing.
    successes = [r for r in results if isinstance(r, Edition)]
    conflicts = [r for r in results if isinstance(r, PublicationConflict)]
    assert len(successes) == (2 if identical else 1), results
    assert len(conflicts) == (0 if identical else 1), results
    assert await counts(store) == ((1 if initial else 2), 1)
    winner = await store.head(DAY, 'banko')
    assert all(r.document == winner.document for r in successes)
    print('COUPON_CAS '+json.dumps(dict(initial=initial, identical=identical,
        transactions=arrived, successful_replies=len(successes), conflicts=len(conflicts),
        committed_editions=(await counts(store))[0])))


async def test_postgres_wrong_cas_and_forged_chain_leave_no_orphans(store):
    first = freeze()
    await store.append(first, expected_digest=None, now=NOW)
    second = revised(first)
    with pytest.raises(PublicationConflict):
        await store.append(second, expected_digest='f'*64, now=NOW)
    invalid = forged(second, lambda d: d.update(revision=3))
    with pytest.raises(ValueError):
        await store.append(invalid, expected_digest=first.as_dict()['content_digest'], now=NOW)
    assert await counts(store) == (1, 1)


async def test_postgres_absent_no_eligible_incomplete_and_expiry_boundary(store):
    assert await store.head(DAY, 'banko') is None
    first = freeze()
    # New insert at the expiry boundary is rejected without touching the head.
    with pytest.raises(ValueError, match='stale'):
        await store.append(first, expected_digest=None, now=NOW+timedelta(minutes=15))
    assert await counts(store) == (0, 0)
    await store.append(first, expected_digest=None, now=NOW+timedelta(minutes=15, microseconds=-1))
    assert project(await store.head(DAY, 'banko'), NOW+timedelta(minutes=15))['status'] == 'stale'
    for kind, reason, state in [('balanced', 'no_qualified_candidates', 'no_eligible'),
                                ('combined_6_7', 'search_budget_exhausted', 'incomplete')]:
        edition = propose(dict(kind=kind, status='unavailable', items=[], reason=reason),
                          day=DAY, now=NOW, model_version='synthetic-v1')
        await store.append(edition, expected_digest=None, now=NOW)
        assert project(await store.head(DAY, kind), NOW)['status'] == state
    assert await store.head(DAY, 'combined_8_10') is None


@pytest.mark.parametrize('statement', [
    "UPDATE {edition} SET model_version='changed'",
    'DELETE FROM {edition}',
    'TRUNCATE {edition} CASCADE',
    'DELETE FROM {head}',
    'TRUNCATE {head}',
])
async def test_postgres_database_append_only_guards(store, statement):
    first = freeze()
    await store.append(first, expected_digest=None, now=NOW)
    with pytest.raises(IntegrityError):
        async with store.engine.begin() as conn:
            await conn.execute(text(statement.format(edition=store._table('coupon_edition'), head=store._table('coupon_head'))))
    assert await counts(store) == (1, 1)
    assert (await store.head(DAY, 'banko')).document == first.document


async def test_postgres_head_cannot_rewind_or_cross_scope(store):
    first = freeze()
    second = revised(first)
    await store.append(first, expected_digest=None, now=NOW)
    await store.append(second, expected_digest=first.as_dict()['content_digest'], now=NOW)
    for clause in ('edition_id=:id', "kind='balanced'"):
        with pytest.raises(IntegrityError):
            async with store.engine.begin() as conn:
                await conn.execute(text(f'UPDATE {store._table("coupon_head")} SET {clause}'), {'id': first.as_dict()['edition_id']})
    assert (await store.head(DAY, 'banko')).document == second.document


@pytest.mark.parametrize('changed', ['content_digest', 'content', 'day', 'kind'])
async def test_postgres_digest_and_byte_limit_constraints_even_on_direct_insert(store, changed):
    first = freeze()
    if changed == 'content':
        first = forged(first, lambda d: d['items'][0]['match'].update(home_team='ş'*MAX_CONTENT_BYTES))
    d = first.as_dict()
    content = canonical({k: v for k, v in d.items() if k not in ('edition_id', 'content_digest')})
    params = dict(storage_id=uuid4(), day=date.fromisoformat(d['day']), kind=d['kind'],
        revision=1, content_digest=d['content_digest'], edition_id=d['edition_id'],
        previous_id=None, first_id=d['edition_id'], captured_as_of=NOW,
        model_version=d['model_version'], content=content)
    if changed == 'content_digest':
        params.update(content_digest='f'*64, edition_id='ce1:'+'f'*64, first_id='ce1:'+'f'*64)
    elif changed == 'day':
        params['day'] += timedelta(days=1)
    elif changed == 'kind':
        params['kind'] = 'balanced'
    # Empty table: no unique collision can mask the intended check failure.
    with pytest.raises(IntegrityError) as error:
        async with store.engine.begin() as conn:
            await conn.execute(text(f'INSERT INTO {store._table("coupon_edition")} ({",".join(params)}) VALUES ({",".join(":"+k for k in params)})'), params)
    if changed == 'content':
        assert 'coupon_content_byte_budget' in str(error.value)
    elif changed == 'content_digest':
        assert 'coupon_content_digest_matches' in str(error.value)
    assert await counts(store) == (0, 0)


@pytest.mark.parametrize('market', ['result', 'first_half_goals', 'second_half_goals'])
async def test_postgres_frozen_settlement_missing_ft_ht_requires_evidence(store, market):
    from src.analytics.performance import settle
    from src.models import Match
    product = deepcopy(products()['banko'])
    item = product['items'][0]
    if market != 'result':
        item['recommendation'].update(id='over_0_5_'+market, market=market, selection='over_0_5')
        item['quote'].update(line=.5, period='first_half' if market == 'first_half_goals' else 'second_half')
    edition = freeze(product=product)
    await store.append(edition, expected_digest=None, now=NOW)
    loaded = await store.head(DAY, 'banko')
    match = Match(status='finished', ft_home=None, ft_away=None, ht_home=None, ht_away=None)
    assert settle(match, None, loaded.as_dict()['items'][0]['recommendation'])[0] == 'awaiting_data'
    assert loaded.document == edition.document


async def test_postgres_capacity_and_index_evidence(store):
    sizes = {}
    for kind in KINDS:
        edition = freeze(kind)
        sizes[kind] = len(content_text(edition).encode('utf-8'))
        await store.append(edition, expected_digest=None, now=NOW)
    async with store.engine.begin() as conn:
        row_bytes = (await conn.execute(text(f'SELECT kind, pg_column_size(e) AS bytes FROM {store._table("coupon_edition")} e ORDER BY kind'))).mappings().all()
        relations = {name: await conn.scalar(text('SELECT pg_total_relation_size(:name)'),
            {'name': f'{store.schema}.{name}'}) for name in ('coupon_edition', 'coupon_head')}
        # This proves index availability, not a production latency benchmark.
        await conn.execute(text('SET LOCAL enable_seqscan=off'))
        plan = await conn.scalar(text(f'EXPLAIN (FORMAT JSON) SELECT edition_id FROM {store._table("coupon_head")} WHERE day=:day AND kind=:kind'),
            dict(day=date.fromisoformat(DAY), kind='banko'))
    assert 'Index' in plan[0]['Plan']['Node Type']
    assert max(sizes.values()) <= MAX_CONTENT_BYTES
    print('COUPON_CAPACITY '+json.dumps(dict(canonical_bytes=sizes,
        row_bytes=[dict(row) for row in row_bytes], relation_bytes=relations,
        max_content_bytes=MAX_CONTENT_BYTES, head_plan=plan), default=str))

@pytest.mark.parametrize('initial', [True, False])
async def test_postgres_failure_between_edition_insert_and_head_cas_rolls_back(store, initial):
    first = freeze()
    if not initial:
        await store.append(first, expected_digest=None, now=NOW)
    candidate = first if initial else revised(first)
    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if store._table('coupon_head') in statement and statement.lstrip().startswith(('INSERT', 'UPDATE')):
            raise RuntimeError('synthetic crash after edition insert')
    event.listen(store.engine.sync_engine, 'before_cursor_execute', interrupt)
    try:
        with pytest.raises(RuntimeError, match='synthetic crash'):
            await store.append(candidate, expected_digest=None if initial else first.as_dict()['content_digest'], now=NOW)
    finally:
        event.remove(store.engine.sync_engine, 'before_cursor_execute', interrupt)
    assert await counts(store) == ((0, 0) if initial else (1, 1))
    await store.close()
    await store.append(candidate, expected_digest=None if initial else first.as_dict()['content_digest'], now=NOW)
    assert (await store.head(DAY, 'banko')).document == candidate.document
