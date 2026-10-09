from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from src.models import Match,League,MatchStatistics,PredictionSnapshot,ScraperJob
from src.analytics.performance import settle,capture
from tests.test_predictions import seed,NOW
from tests.test_match_statistics import captured
from src.scrapers.goaloo.competitions import verified_snapshot,upsert_competitions,apply_production_scope,PRODUCTION_LEAGUE_IDS


@pytest.mark.parametrize('market,selection,expected',[
 ('goals','over_2_5','won'),('goals','under_3_5','lost'),('btts','yes','won'),('btts','no','lost'),
 ('result','home','won'),('result','draw','lost'),('double_chance','1x','won'),('double_chance','x2','lost'),
 ('asian_handicap','home_minus_0_5','won'),('asian_handicap','away_plus_0_5','lost'),
 ('corners','over_9_5','won'),('corners','under_11_5','won'),('cards','over_4_5','won'),('cards','under_6_5','won')])
def test_real_final_settlement_rules(market,selection,expected):
 match=SimpleNamespace(status='finished',ft_home=3,ft_away=1)
 stats=SimpleNamespace(is_final=True,home_corners=6,away_corners=5,home_yellow_cards=2,away_yellow_cards=3,home_red_cards=0,away_red_cards=1)
 assert settle(match,stats,dict(market=market,selection=selection))[0]==expected
 stats.away_red_cards=None
 if market=='cards':assert settle(match,stats,dict(market=market,selection=selection))[0]=='awaiting_data'
 match.status='live';assert settle(match,stats,dict(market=market,selection=selection))[0]=='pending'


async def test_forward_ledger_filters_idempotency_no_retrospective_invention(api,db,monkeypatch):
 import src.api.routes as routes
 monkeypatch.setattr(routes,'utcnow',lambda:NOW)
 id,league,_,_,_=await seed(db)
 assert (await api.get('/api/market-performance')).json()['summary']['success_rate'] is None
 picks=(await api.get('/api/predictions/best')).json()['items'];assert picks
 await api.get('/api/predictions/best')
 async with db.session() as session:
  snapshot=await session.get(PredictionSnapshot,id);original=deepcopy(snapshot.picks)
  assert await session.scalar(select(func.count(PredictionSnapshot.match_id)))==1
  match=await session.get(Match,id);match.status='finished';match.ft_home=3;match.ft_away=1
  await session.commit()
 result=(await api.get('/api/market-performance')).json()
 assert result['summary']['settled']==len(picks) and result['summary']['won']+result['summary']['lost']==len(picks)
 assert result['summary']['pending']==0
 exact=original[0]['id']
 filtered=(await api.get('/api/market-performance',params={'exact_market':exact,'league':league,'date_from':'2026-10-08','date_to':'2026-10-08'})).json()
 assert filtered['total']==1 and filtered['items'][0]['recommendation']['id']==exact
 assert (await api.get('/api/market-performance?market=cards')).json()['total']==0
 assert (await api.get('/api/market-performance?date_from=2026-10-09')).json()['total']==0
 assert (await api.get('/api/market-performance?date_from=2026-10-10&date_to=2026-10-08')).status_code==400
 assert (await api.get('/api/market-performance?offset=1&limit=1')).json()['items']==result['items'][1:2]
 async with db.session() as session:
  match=await session.get(Match,id);assert not await capture(session,match,original,NOW+timedelta(days=10))
  assert (await session.get(PredictionSnapshot,id)).picks==original
  (await session.get(League,league)).enabled=False;await session.commit()
 assert (await api.get('/api/market-performance')).json()['total']==0
 assert (await api.get(f'/api/matches/{id}')).status_code==404
 assert (await api.get(f'/api/matches/{id}/odds')).status_code==404
 assert (await api.get(f'/api/leagues/{league}/seasons')).status_code==404


async def test_selected_confidence_probability_and_team_filters(api,db,monkeypatch):
 import src.api.routes as routes
 monkeypatch.setattr(routes,'utcnow',lambda:NOW)
 await seed(db)
 assert (await api.get('/api/predictions?min_probability=1')).json()['items']==[]
 assert (await api.get('/api/predictions/best?team=nonexistent')).json()['items']==[]
 for confidence in ['high','medium']:
  page=(await api.get('/api/predictions/best',params={'confidence':confidence})).json()
  assert all(r['recommendation']['confidence']==confidence for r in page['items'])
  cards=(await api.get('/api/predictions',params={'confidence':confidence})).json()['items']
  assert all(r['confidence']==confidence for card in cards for r in card['recommendations'])
 assert (await api.get('/api/predictions?min_probability=101')).status_code==422


async def test_eight_league_rollout_scope_and_no_duplicate_queue(db,monkeypatch):
 import src.jobs.worker as worker
 monkeypatch.setattr(worker,'database',db)
 async with db.session() as session:
  await upsert_competitions(session,verified_snapshot());await apply_production_scope(session);await apply_production_scope(session);await session.commit()
  enabled=set(await session.scalars(select(League.external_id).where(League.enabled.is_(True))))
  assert enabled==set(PRODUCTION_LEAGUE_IDS)
 first=await worker.enqueue_stats_rollout();second=await worker.enqueue_stats_rollout()
 assert len(first)==8 and [j.id for j in first]==[j.id for j in second]
 assert [j.priority for j in first]==sorted(j.priority for j in first)
 async with db.session() as session:assert await session.scalar(select(func.count(ScraperJob.id)))==8


async def test_partial_source_does_not_erase_observations_and_old_partial_can_retry(db,monkeypatch):
 from src.jobs.storage import store_statistics
 import src.jobs.worker as worker
 monkeypatch.setattr(worker,'database',db)
 id,_,_,_,_=await seed(db)
 async with db.session() as session:
  match=await session.get(Match,id);match.status='finished'
  data=deepcopy(captured());data['raw']['match_id']=match.external_match_id
  await store_statistics(session,match,data);await session.commit()
  partial=deepcopy(data);partial['statistics']={};partial['collection_status']='unavailable';partial['is_final']=False
  row=await store_statistics(session,match,partial);assert row.home_corners==7 and row.is_final and row.collection_status=='available'
  row.updated_at=NOW-timedelta(days=8);await session.commit()
 monkeypatch.setattr(worker,'utcnow',lambda:NOW)
 job=await worker.enqueue('stats_backfill');await worker.discover(job.id,None)
 async with db.session() as session:
  from src.models import JobItem
  assert await session.scalar(select(JobItem.id).where(JobItem.job_id==job.id,JobItem.match_id==id))


def test_dark_market_markup_and_runtime():
    from pathlib import Path
    import shutil,subprocess
    html=Path('src/api/dashboard.html').read_text(encoding='utf-8')
    css=Path('src/api/dashboard-market.css').read_text(encoding='utf-8')
    assert '--app-bg:#EFF3F8' in css and 'width:100vw' in css
    for id in ['market-picks','market-category-cards','performance-section','performance-markets','analysis-tab-summary']:
        assert f'id="{id}"' in html
    assert '<aside' not in html and 'Arsenal' not in html
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable')
    subprocess.run([node,'tests/market_runtime.cjs'],check=True,capture_output=True,text=True)


@pytest.mark.parametrize('league,match,corners,reds',[(11,2594896,(2,8),(None,None)),(16,2592240,(8,1),(0,1)),(23,2611656,(6,1),(None,None))])
def test_additional_real_league_sources(league,match,corners,reds):
    from pathlib import Path
    from src.scrapers.goaloo.statistics import parse_statistics
    data=parse_statistics(Path(f'tests/fixtures/match_statistics_{league}.html').read_text(encoding='utf-8'),match,league)
    assert (data['statistics']['home_corners'],data['statistics']['away_corners'])==corners
    assert (data['statistics']['home_red_cards'],data['statistics']['away_red_cards'])==reds
    assert data['referee'] is None and data['is_final']


def test_full_corner_card_line_ranges():
    from src.analytics.match_statistics import count_prediction
    from tests.test_match_statistics import metric_rows
    assert set(count_prediction(metric_rows(75),1,2,'corners')['over_probabilities'])=={'7.5','8.5','9.5','10.5','11.5'}
    assert set(count_prediction(metric_rows(75),1,2,'cards')['over_probabilities'])=={'2.5','3.5','4.5','5.5','6.5'}


async def test_live_values_never_become_final_counts(db):
    from src.jobs.storage import store_statistics
    id,_,_,_,_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,id)
        partial=deepcopy(captured());partial['raw']['match_id']=match.external_match_id;partial['is_final']=False
        row=await store_statistics(session,match,partial);assert row.home_corners==7 and not row.is_final
        final=deepcopy(partial);final['is_final']=True;final['statistics']={}
        row=await store_statistics(session,match,final);assert row.home_corners is None and row.is_final


@pytest.mark.skipif(not __import__('os').getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL not configured')
async def test_postgres_scope_migration_and_concurrent_immutable_capture(monkeypatch):
    import os,uuid,asyncio
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    from src.analytics.recommendations import rank_candidate
    from tests.test_recommendations import candidate
    from src.analytics.performance import performance
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='market_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await db.initialize()
        async with db.session() as session:await apply_production_scope(session);await session.commit()
        id,_,_,_,_=await seed(db)
        import src.jobs.worker as worker
        monkeypatch.setattr(worker,'database',db)
        rollouts=await asyncio.gather(*(worker.enqueue_stats_rollout() for _ in range(4)))
        assert all([j.id for j in rows]==[j.id for j in rollouts[0]] for rows in rollouts)
        async with db.session() as session:
            assert await session.scalar(select(func.count(ScraperJob.id)).where(ScraperJob.kind=='stats_backfill'))==8
        pick=rank_candidate(candidate('goals','goal_environment',.68,'over_2_5'))
        async def publish():
            async with db.session() as session:
                match=await session.get(Match,id);created=await capture(session,match,[pick],NOW);await session.commit();return created
        assert sum(await asyncio.gather(*(publish() for _ in range(6))))==1
        async with db.session() as session:
            assert await session.scalar(select(func.count(PredictionSnapshot.match_id)))==1
            assert set(await session.scalars(select(League.external_id).where(League.enabled.is_(True))))==set(PRODUCTION_LEAGUE_IDS)
            assert (await performance(session))['summary']['pending']==1
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


async def test_explicit_live_stats_refresh_is_not_lost_in_history_filter(db,monkeypatch):
    import src.jobs.worker as worker
    from src.models import JobItem
    monkeypatch.setattr(worker,'database',db)
    id,_,_,_,_=await seed(db)
    async with db.session() as session:
        (await session.get(Match,id)).status='live';await session.commit()
    job=await worker.enqueue('stats_backfill',match_id=id);await worker.discover(job.id,None)
    async with db.session() as session:
        assert await session.scalar(select(JobItem.id).where(JobItem.job_id==job.id,JobItem.match_id==id))
