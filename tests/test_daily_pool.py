"""Daily census must include fixtures omitted from display and later pages."""
from copy import deepcopy
from datetime import timedelta
import os
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema,DropSchema
from src.db import Database
from src.models import Match,PredictionBoard,MarketOdds,League,Season
from src.analytics.saved_coupons import automatic
from src.analytics.coupon import quote_draft,DraftRequest
from tests.test_automatic_telegram import many_matches,NOW
from tests.odds_fixture import move_observations


async def pool_fixture(db, eligible=4):
    await many_matches(db,20)
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard))
        items=deepcopy(board.payload['items']);records=[]
        for index,item in enumerate(items):
            candidates=deepcopy(item['recommendations'])
            if index<20-eligible:
                for c in candidates:c['probability']=.69
            # Qualified priced candidates are beyond the two UI suggestions.
            displayed=deepcopy(candidates)
            for c in displayed:c['qualifies']=False
            item['recommendations']=displayed
            records.append(dict(match=deepcopy(item['match']),candidates=candidates))
        # The last fixtures belong to another enabled league. No league/page cutoff.
        league=League(external_id=999002,name='Second test league',country='Test',enabled=True)
        session.add(league);await session.flush()
        season=Season(league_id=league.id,season_name='2026-2027');session.add(season);await session.flush()
        for item in records[-2:]:
            match=await session.get(Match,item['match']['id']);match.league_id=league.id;match.season_id=season.id
            item['match']['league_id']=league.id
        session.add(PredictionBoard(league_id=league.id,model_version=board.model_version,generated_at=NOW,expires_at=NOW+timedelta(minutes=10),source_revision='daily-test',payload=dict(items=[],evaluated=2,availability=[],engine_analysis=[],coupon_records=records[-2:])))
        board.payload={**board.payload,'items':items[:-2],'coupon_records':records[:-2]}
        await session.commit()


async def verify_pool(db,eligible):
    await pool_fixture(db,eligible)
    async with db.session() as session:
        row=await automatic(session,NOW);await session.commit()
        d=session.info['coupon_diagnostics']
        assert d['inspected_matches']==20 and d['eligible_matches']==eligible
        assert d['candidate_coverage_complete']
        assert sum(d['match_rejections'].values())+d['eligible_matches']==20
        assert d['match_rejections'].get('quality_rejected',0)==20-eligible
        assert len(row.payload['items'])==eligible if eligible else row is None
        if row:
            picks=row.payload['items']
            assert len({i['match']['id'] for i in picks})==eligible
            draft=DraftRequest(selections=[dict(match_id=i['match']['id'],recommendation_id=i['recommendation']['id'],bookmaker=i['quote']['bookmaker']) for i in picks])
            assert (await quote_draft(session,draft,NOW))['eligible']
            if eligible==4:
                assert len({i['match']['league_id'] for i in picks})==2
                assert row.payload['combined_odds']>5
            # No stale cached ticket survives source expiry, even with a fresh board.
            for price in await session.scalars(select(MarketOdds)):move_observations(price,NOW-timedelta(seconds=901))
            await session.commit()
            assert await automatic(session,NOW) is None
            assert session.info['coupon_diagnostics']['match_rejections']['price_rejected']==eligible


@pytest.mark.parametrize('eligible',[4,1,0])
async def test_twenty_fixture_pool_and_honest_partial_diagnosis(db,eligible):
    await verify_pool(db,eligible)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_twenty_fixture_pool():
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='daily_pool_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await verify_pool(db,4)
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_readonly_export_replay():
    import gzip,json
    from pathlib import Path
    from scripts.audit_coupon_snapshot import replay
    snapshot=json.loads(gzip.decompress(Path('docs/daily-coupon/2026-10-15-snapshot.json.gz').read_bytes()))
    result=await replay(snapshot)
    d=result['diagnostics']
    assert result['database']=='postgresql' and result['http_status']==200
    assert d['inspected_matches']==36 and d['eligible_matches']==1
    assert d['selected_matches']==0  # The sole real quote has negative model value.
    assert d['match_rejections']=={'insufficient_prematch_data':26,'price_rejected':3,'quality_rejected':6}
    assert d['source_coverage']['priced_matches']==36
    assert d['source_coverage']['fresh_price_matches']==5
    assert result['automatic_coupon'] is None


async def test_full_pool_rejects_stale_analysis_reschedule_and_new_day(db):
    await pool_fixture(db,4)
    async with db.session() as session:
        row=await automatic(session,NOW);await session.commit()
        assert row.payload['coupon_date']=='2026-10-08'
        match=await session.get(Match,row.payload['items'][0]['match']['id'])
        match.kickoff_at+=timedelta(minutes=1);await session.commit()
        result=await automatic(session,NOW)
        assert len(result.payload['items'])==3
        assert await automatic(session,NOW+timedelta(days=1)) is None
        for b in await session.scalars(select(PredictionBoard)):b.expires_at=NOW
        await session.commit()
        assert await automatic(session,NOW) is None
        assert session.info['coupon_diagnostics']['match_rejections']=={'analysis_stale':20}


async def test_worker_publishes_all_ranked_candidates(db):
    from tests.test_predictions import seed
    match_id,league,*_=await seed(db)
    async with db.session() as session:
        board=await session.get(PredictionBoard,league)
        record=next(r for r in board.payload['coupon_records'] if r['match']['id']==match_id)
        assert len(record['candidates'])>2
        assert len(board.payload['items'][0]['recommendations'])<=2
        assert all('qualifies' in c and 'rejection_reasons' in c for c in record['candidates'])


async def test_calendar_rollover_creates_new_daily_record_and_keeps_old(db):
    from src.models import SavedCoupon
    await pool_fixture(db,1)
    async with db.session() as session:
        previous=await automatic(session,NOW);await session.commit()
        original=deepcopy(previous.payload)
        next_day=NOW+timedelta(days=1)
        for b in await session.scalars(select(PredictionBoard)):
            b.generated_at=next_day;b.expires_at=next_day+timedelta(minutes=10)
            data=deepcopy(b.payload)
            for record in data['coupon_records']:
                match=await session.get(Match,record['match']['id'])
                match.kickoff_at+=timedelta(days=1)
                record['match']['kickoff_at']=match.kickoff_at.replace(tzinfo=NOW.tzinfo).isoformat()
            b.payload=data
        for price in await session.scalars(select(MarketOdds)):move_observations(price,next_day)
        await session.commit()
        new=await automatic(session,next_day);await session.commit()
        assert new.id!=previous.id and new.payload['coupon_date']=='2026-10-09'
        assert (await session.get(SavedCoupon,previous.id)).payload==original


async def test_partial_api_reports_all_nineteen_rejections(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await pool_fixture(db,1)
    data=(await api.post('/api/coupons/automatic')).json()
    assert len(data['items'])==1 and data['selection_mode']=='dynamic'
    assert data['diagnostics']['inspected_matches']==20
    assert data['diagnostics']['match_rejections']=={'quality_rejected':19}
    assert data['data_updates']['checked_at'] and data['data_updates']['analysis_generated_at']


def test_candidate_diagnostics_use_the_same_quality_gate():
    from src.analytics.odds_diagnostics import report
    from src.analytics.daily_pool import quality_failures
    candidate=dict(id='home_result',market='result',qualifies=False,probability=.9,reliability=.9,score=.9)
    records=[dict(match=dict(kickoff_at=NOW),recommendations=[candidate])]
    assert report(records,NOW)['daily']['quality_rejected']==1
    candidate.update(qualifies=True,probability=float('nan'))
    assert report(records,NOW)['daily']['quality_rejected']==1
    assert 'coupon_probability' in quality_failures(candidate)


async def test_worker_refresh_includes_unlisted_today_and_preserves_backoff(db,monkeypatch):
    from src.jobs.live import PriorityUpdater
    from src.models import CollectorRetry
    import src.jobs.live as live
    import src.scrapers.goaloo.odds as odds
    await many_matches(db,20)
    async with db.session() as session:
        for b in await session.scalars(select(PredictionBoard)):
            b.payload={**b.payload,'items':[],'coupon_records':[]}
        await session.commit()
        before={o.id:deepcopy(o.raw) for o in await session.scalars(select(MarketOdds))}
    calls=[]
    async def unavailable(client,external,final):
        assert not final;calls.append(external);return {}
    monkeypatch.setattr(odds,'fetch_odds',unavailable)
    monkeypatch.setattr(live,'utcnow',lambda:NOW)
    updater=PriorityUpdater(db)
    for index in range(20):
        await updater.upcoming_quotes(None)
        assert len(calls)==index+1  # A single source request per checkpoint.
    assert len(set(calls))==20
    await updater.upcoming_quotes(None)
    assert len(calls)==20  # Empty source responses are durably backed off.
    async with db.session() as session:
        assert len((await session.scalars(select(CollectorRetry))).all())==20
        assert {o.id:o.raw for o in await session.scalars(select(MarketOdds))}==before
