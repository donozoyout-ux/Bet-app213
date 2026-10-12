"""Issue 39: captured status annotations, strict provenance and checkpoint counts."""
import json
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace
import httpx
import pytest
from sqlalchemy import select
from src.models import ScraperJob
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.analytics.odds_matching import match_odds_for_candidate
from tests.test_odds_matching import MockOdds1X2, MockAsianTotals


def test_captured_status_annotations_and_invalid_neighbor():
    rows = list(json.loads((Path(__file__).parent/'fixtures/goaloo_status_annotations.json').read_text(encoding='utf-8')).values())
    bad = list(rows[0]); bad[0] = 99; bad[2] = -1
    payload = {'TeamInfo': [[r[i], str(r[i])] for r in rows for i in (4, 5)],
               'ScheduleList': {'R_1': [*rows, bad]}}
    errors = []
    parsed = list(parse_matches(payload, 1, errors))
    assert {r['external_match_id']: r['status'] for r in parsed} == {3013719:'abandoned', 3023443:'postponed'}
    assert all(r['ft_home'] is None and r['ft_away'] is None for r in parsed)
    assert [r['external_match_id'] for r in errors] == ['99']


def test_persisted_price_without_field_timestamp_is_unknown():
    now = datetime.now(timezone.utc)
    row = MockOdds1X2(2, 3, 4, 2, 3, 4, updated_at=now)
    row.raw = {'source':'actual_response'}
    result = match_odds_for_candidate({'market':'result','selection':'home','probability':.8},
        1, 'scheduled', odds_1x2_rows=[(row,'Bet365')], cutoff=now)
    assert result['has_odds'] and result['observed_at'] is None
    assert not result['coupon_eligible'] and result['reason']=='odds_unknown'


def test_corner_model_85_never_uses_source_95():
    row = MockAsianTotals(9.5, 2, 2, 9.5, 2, 2, market='corners')
    result = match_odds_for_candidate({'market':'corners','selection':'over_8_5','probability':.8},
        1, 'scheduled', period_odds_rows=[(row,'Bet365')])
    assert result['best_price'] is None and result['reason']=='line_mismatch'


async def test_failed_endpoint_cooldown_is_parameter_specific(monkeypatch):
    import src.scrapers.goaloo.client as module
    monkeypatch.setattr(module, 'settings', replace(module.settings, request_interval=0, retries=1))
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'code':1002})
    async with GoalooClient(httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(SourceError):await client.get('https://example.test/odds', {'h':0})
        assert len(calls)==1
        with pytest.raises(SourceError):await client.get('https://example.test/odds', {'h':1})
        assert len(calls)==2
        client.failed_until.clear()
        with pytest.raises(SourceError):await client.get('https://example.test/odds', {'h':0})
        assert len(calls)==3


async def test_discovery_added_unchanged_and_wrong_league_isolation(db, monkeypatch):
    from tests.test_jobs import install_source
    from tests.test_goaloo import fixture
    from src.jobs.worker import enqueue
    worker = await install_source(monkeypatch, db)
    async def no_priority(*args):pass
    monkeypatch.setattr(worker, 'priority_cycle', no_priority)
    payload = fixture('goaloo_league.json')
    payload['ScheduleList']['R_1'][0][1] = 999
    async def source(*args):return payload
    monkeypatch.setattr(worker, 'season_data', source)
    first = await enqueue('backfill')
    await worker.discover(first.id, None)
    async with db.session() as session:
        job = await session.get(ScraperJob, first.id)
        metrics = job.discovery_metrics
        assert sum(r['added'] for r in metrics.values())==9
        assert sum(r['errors'] for r in metrics.values())==1
        assert job.discovery_finished_at is not None
        job.discovery_complete=False
        await session.commit()
    await worker.discover(first.id, None)
    async with db.session() as session:
        job = await session.get(ScraperJob, first.id)
        assert sum(r['added'] for r in job.discovery_metrics.values())==0
        assert sum(r['unchanged'] for r in job.discovery_metrics.values())==9
        assert sum(r['errors'] for r in job.discovery_metrics.values())==1


async def test_source_rows_are_field_observations_not_row_update_times(db):
    from tests.test_goaloo import fixture
    from src.jobs.storage import store_match, store_odds
    from src.models import League, Odds1X2
    from src.scrapers.goaloo.odds import parse_odds
    from src.analytics.odds_diagnostics import source_report
    async with db.session() as session:
        league=await session.scalar(select(League))
        match=await store_match(session, league.id, '2024-2025', next(parse_matches(fixture('goaloo_league.json'), 1)))
        await store_odds(session, match, parse_odds(fixture('goaloo_odds.json'),True), True)
        await session.commit()
        before=await source_report(session, datetime.now(timezone.utc))
        result=next(r for r in before if r['market']=='result' and r['bookmaker']=='Bet365')
        assert result['latest_fields']['home']=={'priced':1,'observed':1,'fresh':1}
        for row in await session.scalars(select(Odds1X2)):
            row.raw={}
        await session.commit()
        after=await source_report(session, datetime.now(timezone.utc))
        result=next(r for r in after if r['market']=='result' and r['bookmaker']=='Bet365')
        assert result['latest_fields']['home']=={'priced':1,'observed':0,'fresh':0}


async def test_odds_api_exposes_period_source_and_field_provenance(api, db):
    from tests.test_coupon import coupon_seed
    ids=await coupon_seed(db)
    response=await api.get(f'/api/matches/{ids[0]}/odds')
    assert response.status_code==200
    row=next(r for r in response.json() if r['market']=='first_half_goals' and r['bookmaker']=='Bet365')
    assert row['period']=='first_half' and row['source']=='goaloo'
    assert row['latest']['line']==1.5
    assert row['field_observed_at']['latest_over'] is not None
    assert row['field_observed_at']['opening_over'] is None


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'), reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_source_aggregation_and_additive_discovery_migration():
    from src.db import Database
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.schema import CreateSchema, DropSchema
    db=Database(os.environ['TEST_POSTGRES_URL'])
    schema='issue39_test_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine, expire_on_commit=False)
        await db.initialize()
        async with db.engine.begin() as conn:
            await conn.execute(text(f'ALTER TABLE "{schema}".scraper_jobs DROP COLUMN discovery_metrics'))
            await conn.execute(text(f'ALTER TABLE "{schema}".scraper_jobs DROP COLUMN discovery_finished_at'))
        await db.initialize()
        await db.initialize()
        await test_source_rows_are_field_observations_not_row_update_times(db)
        async with db.session() as session:
            # Selecting the mapped columns verifies the existing-schema migration.
            assert list(await session.scalars(select(ScraperJob)))==[]
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        await db.close()
