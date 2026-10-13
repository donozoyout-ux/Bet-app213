"""Exact quote diagnostics, bounded scope, strict home prices and empty freshness."""
from copy import deepcopy
from datetime import datetime, timedelta
import os
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from src.db import Database
from src.models import Match, MarketOdds, Bookmaker, PredictionBoard
from src.analytics.coupon import current_odds
from src.analytics.odds_diagnostics import source_report
from src.analytics.odds_matching import price_matches_line
from tests.test_coupon import coupon_seed, NOW
from tests.odds_fixture import observed_prices, move_observations


async def scope_check(db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    ids = await coupon_seed(db)
    async with db.session() as session:
        historical = await session.scalar(select(Match).where(Match.status == 'finished'))
        book = await session.scalar(select(Bookmaker).where(Bookmaker.name == 'Bet365'))
        session.add(MarketOdds(match_id=historical.id, bookmaker_id=book.id,
            market='corners', period='full_time', latest_line=99.5, latest_over=1.8,
            updated_at=NOW, raw={**observed_prices(NOW, 'latest_over'), 'history_blob': 'x'*100000}))
        await session.commit()
    queries = []
    def record(conn, cursor, statement, params, context, many):
        if 'JOIN bookmakers' in statement and 'SELECT' in statement:
            queries.append(statement)
    event.listen(db.engine.sync_engine, 'before_cursor_execute', record)
    try:
        async with db.session() as session:
            data = await routes.odds_coverage(session=session)
    finally:
        event.remove(db.engine.sync_engine, 'before_cursor_execute', record)
    assert data['source_scope'] == 'enabled_upcoming_7_days' and data['source_match_count'] == 2
    assert not any(row['line'] == 99.5 for row in data['source_rows'])
    assert queries and all('match_id IN (' in query for query in queries)
    assert set(data['rejections']['corners']) >= {'no_source_rows', 'line_mismatch', 'stale', 'unsupported_bookmaker', 'missing_stage_price'}
    async with db.session() as session:
        row = await session.scalar(select(MarketOdds).where(MarketOdds.match_id == ids[0], MarketOdds.market == 'corners', MarketOdds.bookmaker_id == book.id))
        stamp = NOW.astimezone(ZoneInfo('Europe/Istanbul')).isoformat()
        row.raw = {'_field_observed_at': {'latest_over': stamp, 'latest_under': stamp},
            '_field_lines': {'latest_over': 8.5, 'latest_under': 9.5}}
        await session.commit()
        rows = await source_report(session, NOW, match_ids=[ids[0]])
        result = next(r for r in rows if r['market'] == 'corners' and r['bookmaker'] == 'Bet365')
        assert result['latest_fields']['over']['fresh'] == 0
        assert result['latest_fields']['under']['fresh'] == 1
        assert result['last_observed_at'] == NOW


async def test_diagnostic_scope_and_exact_field_provenance(db, monkeypatch):
    await scope_check(db, monkeypatch)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_diagnostic_scope_and_exact_field_provenance(monkeypatch):
    db = Database(os.environ['TEST_POSTGRES_URL']); schema = 'coupon_e2e_' + uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        db.engine = db.engine.execution_options(schema_translate_map={None: schema})
        db.sessions = async_sessionmaker(db.engine, expire_on_commit=False)
        await db.initialize()
        await scope_check(db, monkeypatch)
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await db.close()


@pytest.mark.parametrize('value', ['9.5', 'invalid', None, True, float('nan'), float('inf')])
def test_unverified_field_lines_are_not_equivalent_prices(value):
    row = SimpleNamespace(latest_line=9.5, raw={'_field_lines': {'latest_over': value}})
    assert not price_matches_line(row, 'latest', 'over', 9.5)


async def test_unsupported_bookmaker_is_measured_not_converted_to_missing_source(db):
    ids = await coupon_seed(db)
    async with db.session() as session:
        rows = list(await session.scalars(select(MarketOdds).where(MarketOdds.match_id == ids[0], MarketOdds.market == 'corners')))
        for row in rows:
            await session.delete(row)
        book = Bookmaker(name='Unsupported', external_id=999)
        session.add(book); await session.flush()
        session.add(MarketOdds(match_id=ids[0], bookmaker_id=book.id, market='corners', period='full_time',
            latest_line=9.5, latest_under=4.0, updated_at=NOW, raw=observed_prices(NOW, 'latest_under')))
        await session.flush()
        board = await session.scalar(select(PredictionBoard))
        rec = deepcopy(next(r for r in board.payload['items'][0]['recommendations'] if r['market'] == 'corners'))
        await current_odds(session, {ids[0]: [rec]}, NOW)
        assert rec['verified_odds']['reason'] == 'unsupported_bookmaker'
        assert not rec['verified_odds']['has_odds'] and not rec['verified_odds'].get('coupon_eligible')


async def test_home_priced_only_never_promotes_stale_or_unpriced_models(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    ids = await coupon_seed(db)
    page = (await api.get('/api/predictions?priced_only=true')).json()
    assert len(page['items']) == 2
    assert all(r['verified_odds']['coupon_eligible'] for i in page['items'] for r in i['recommendations'])
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):
            move_observations(row, NOW-timedelta(minutes=16))
        await session.commit()
    assert (await api.get('/api/predictions?priced_only=true')).json()['items'] == []
    assert (await api.get('/api/predictions')).json()['items']  # model-only view remains explicit


async def test_empty_daily_coupon_exposes_real_update_times(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await coupon_seed(db)  # These fixtures are tomorrow; no forced daily filler.
    data = (await api.post('/api/coupons/automatic')).json()
    assert data['items'] == [] and data['reason'] == 'no_today_candidates'
    assert data['data_updates']['analysis_generated_at']
    assert data['data_updates']['price_observed_at'] is None  # no daily source was examined
    assert data['data_updates']['checked_at']


async def test_empty_daily_stale_quote_reports_its_original_observation(api, db, monkeypatch):
    import src.api.routes as routes
    from tests.test_automatic_telegram import coupon_seed as daily_seed
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await daily_seed(db)
    stale=NOW-timedelta(minutes=16)
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):
            move_observations(row, stale)
        await session.commit()
    data=(await api.post('/api/coupons/automatic')).json()
    assert data['items']==[] and data['reason']=='price_rejected'
    assert datetime.fromisoformat(data['data_updates']['price_observed_at'].replace('Z','+00:00'))==stale


def test_boolean_handicap_line_cannot_be_numeric_provenance():
    row=SimpleNamespace(latest_line=1.0,raw={'_field_lines':{'latest_home':True}})
    assert not price_matches_line(row,'latest','home',1.0)
