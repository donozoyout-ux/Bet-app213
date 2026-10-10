"""Durable statistics pipeline and qualified count markets, with no fabricated NULLs."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from src.models import League, Match, MatchStatistics, ScraperJob, JobItem, Odds1X2
from src.scrapers.goaloo.client import SourceError
from src.scrapers.goaloo.competitions import verified_snapshot, upsert_competitions, apply_production_scope, PRODUCTION_LEAGUE_IDS
from tests.test_predictions import seed, NOW
from tests.test_match_statistics import captured


async def test_qualified_count_markets_reach_both_prediction_apis(api, db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    target, league, _, _, history = await seed(db)
    async with db.session() as session:
        for id in history:
            # Synthetic regression data with complete published counts; not production validation.
            session.add(MatchStatistics(match_id=id, is_final=True,
                home_corners=2, away_corners=2, home_yellow_cards=1, away_yellow_cards=1,
                home_red_cards=0, away_red_cards=0, updated_at=NOW-timedelta(days=1), raw={}))
        await session.commit()
    from src.analytics.board_cache import refresh_one
    await refresh_one(db, now=NOW, force_league=league)
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert {'corners','cards'} <= {r['market'] for r in data['recommendations']}
    assert len(data['recommendations']) <= 3
    for market in ('corners','cards'):
        assert data['market_availability'][market]['status']=='selected'
        for endpoint in ('/api/predictions','/api/predictions/best'):
            page=(await api.get(endpoint,params={'market':market})).json()
            assert page['items'] and page['availability']['status']=='available'
            filtered=(await api.get(endpoint,params={'market':market,'min_probability':1})).json()
            assert not filtered['items'] and filtered['availability']['status']=='filtered_out'
    async with db.session() as session:
        for row in (await session.scalars(select(MatchStatistics))).all():row.home_red_cards=None
        await session.commit()
    await refresh_one(db, now=NOW, force_league=league)

    page=(await api.get('/api/predictions?market=cards')).json()

    assert page['items']==[] and page['availability']['status']=='insufficient_data'
    detail=page['availability']['markets']['cards']['matches'][0]
    assert detail['model']['league_sample_size']==0
    assert {'code':'league_sample_size','actual':0,'required':20} in detail['model']['insufficient_reasons']


async def test_coverage_reports_final_counts_null_zero_and_disabled_leagues(api, db):
    target, league, _, _, history=await seed(db)
    async with db.session() as session:
        for i,id in enumerate(history[:3]):
            session.add(MatchStatistics(match_id=id,is_final=i!=2,home_corners=0,away_corners=4,
                home_yellow_cards=0,away_yellow_cards=1,home_red_cards=0,away_red_cards=0 if i!=1 else None,raw={}))
        await session.commit()
    result=(await api.get('/api/diagnostics')).json()
    row=result['stats_coverage_by_league'][0]
    assert row['finished_matches']==80 and row['total_stats_rows']==3
    assert row['matches_with_corners']==2 and row['matches_with_cards']==1
    assert row['stats_coverage_percent']=={'corners':2.5,'cards':1.25,'both':1.25}
    performance=(await api.get('/api/market-performance?market=cards')).json()
    assert performance['availability']=='no_saved_prematch_picks'
    assert performance['stats_coverage_by_league']==result['stats_coverage_by_league']
    async with db.session() as session:
        (await session.get(League,league)).enabled=False
        await session.commit()
    assert (await api.get('/api/diagnostics')).json()['stats_coverage_by_league']==[]


async def test_rollout_does_not_mistake_odds_jobs_for_stats_and_retries_failed_only(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    target,league,_,_,history=await seed(db)
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot())
        await apply_production_scope(session)
        # Queue ordering must not depend on mutable catalog priorities.
        for l in (await session.scalars(select(League))).all():l.priority=100-l.id
        session.add(ScraperJob(kind='backfill',league_id=league,status='running'))
        await session.commit()
    jobs=await worker.enqueue_stats_rollout()
    async with db.session() as session:
        assert [(await session.get(League,j.league_id)).external_id for j in jobs]==list(PRODUCTION_LEAGUE_IDS)
        assert all(j.kind=='stats_backfill' for j in jobs)
        j=await session.get(ScraperJob,jobs[0].id)
        j.status='partial';j.finished_at=NOW;j.discovery_complete=True
        session.add_all([JobItem(job_id=j.id,match_id=history[0],status='completed'),JobItem(job_id=j.id,match_id=history[1],status='failed')])
        await session.commit()
    monkeypatch.setattr(worker,'utcnow',lambda:NOW)
    automatic=await worker.enqueue_stats_rollout(automatic=True)
    assert automatic[0].status=='partial'  # no tight retry loop
    resumed=await worker.enqueue_stats_rollout()
    assert [j.id for j in jobs]==[j.id for j in resumed]
    async with db.session() as session:
        assert list(await session.scalars(select(JobItem.status).order_by(JobItem.id)))==['completed','queued']
        assert await session.scalar(select(func.count(ScraperJob.id)).where(ScraperJob.kind=='stats_backfill'))==8


async def test_completed_rollout_rechecks_missing_coverage_and_preserves_observations(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    await seed(db)
    first=(await worker.enqueue_stats_rollout())[0]
    async with db.session() as session:
        j=await session.get(ScraperJob,first.id);j.status='completed'
        await session.commit()
    second=(await worker.enqueue_stats_rollout())[0]
    assert second.id!=first.id  # old successful job is not proof of present coverage
    assert (await worker.enqueue_stats_rollout())[0].id==second.id
    with pytest.raises(ValueError,match='equivalent job'):
        await worker.enqueue('stats_backfill',resume_id=first.id)


async def test_disabled_queued_jobs_do_not_block_enabled_stats(db, monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    async with db.session() as session:
        disabled=League(external_id=999,name='Disabled',country='Test',enabled=False)
        session.add(disabled);await session.flush()
        session.add(ScraperJob(kind='stats_backfill',league_id=disabled.id,status='running',priority=-1))
        await session.commit()
    enabled=(await worker.enqueue_stats_rollout())[0]
    await worker.work(once=True)
    async with db.session() as session:
        assert (await session.get(ScraperJob,enabled.id)).status=='completed'


@pytest.mark.parametrize('odds_fail,history_fail',[(True,False),(False,True)])
async def test_odds_or_history_failure_preserves_real_statistics(db,monkeypatch,odds_fail,history_fail):
    from tests.test_jobs import install_source
    worker=await install_source(monkeypatch,db)
    monkeypatch.setattr(worker,'settings',replace(worker.settings,save_snapshots=history_fail))
    if odds_fail:
        async def bad_odds(*args):raise SourceError('Bookmaker endpoint unavailable')
        monkeypatch.setattr(worker,'fetch_odds',bad_odds)
    async def stats(client,id,league):
        data=deepcopy(captured(16));data['raw']['match_id']=id
        return data
    monkeypatch.setattr(worker,'fetch_statistics',stats)
    async def history(*args):raise SourceError('History unavailable')
    monkeypatch.setattr(worker,'fetch_history',history)
    job=await worker.enqueue('backfill')
    await worker.run_job(job.id)
    async with db.session() as session:
        assert await session.scalar(select(func.count(MatchStatistics.match_id)))==10
        row=await session.scalar(select(MatchStatistics))
        assert (row.home_corners,row.away_corners,row.home_red_cards,row.away_red_cards)==(8,1,0,1)
        if history_fail:assert await session.scalar(select(func.count(Odds1X2.id)))==30


def test_malformed_market_does_not_discard_other_bookmakers():
    from tests.test_goaloo import fixture
    from src.scrapers.goaloo.odds import parse_odds
    payload=deepcopy(fixture('goaloo_odds.json'))
    row=next(r for r in payload['Data']['mixodds'] if int(r['cid'])==3)
    row['ah']={'f':{'u':'invalid'}}
    parsed=parse_odds(payload,True)
    assert parsed['Crown']['ah']['opening_home'] is None
    assert parsed['Crown']['1x2']['opening_home'] is not None
    assert parsed['Bet365']['1x2']['opening_home'] is not None


async def test_running_stats_item_resumes_after_restart_and_disabled_league_pauses(db,monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    target,league,_,_,history=await seed(db)
    job=await worker.enqueue('stats_backfill')
    await worker.discover(job.id,None)
    async with db.session() as session:
        items=(await session.scalars(select(JobItem).where(JobItem.job_id==job.id).order_by(JobItem.id))).all()
        assert items[0].match_id==history[0]  # recent observations first
        for item in items:item.status='completed'
        items[0].status='running'
        (await session.get(ScraperJob,job.id)).status='running'
        await session.commit()
    calls=[]
    async def fetch(client,id,lid):
        calls.append(id)
        data=deepcopy(captured(16));data['raw']['match_id']=id
        return data
    monkeypatch.setattr(worker,'fetch_statistics',fetch)
    await worker.work(once=True)
    assert calls==[990000]
    async with db.session() as session:
        assert (await session.get(ScraperJob,job.id)).status=='completed'
        assert await session.scalar(select(func.count(MatchStatistics.match_id)))==1
        (await session.get(League,league)).enabled=False
        await session.commit()
    await worker.run_job(job.id)
    assert calls==[990000]


async def test_worker_uses_rollout_order_and_does_not_auto_run_disabled_jobs(db,monkeypatch):
    import src.jobs.worker as worker
    monkeypatch.setattr(worker,'database',db)
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot())
        await apply_production_scope(session)
        await session.commit()
    jobs=await worker.enqueue_stats_rollout()
    actual=[]
    async def run(id):
        actual.append(id)
        async with db.session() as session:
            (await session.get(ScraperJob,id)).status='completed'
            await session.commit()
    monkeypatch.setattr(worker,'run_job',run)
    for _ in range(8):await worker.work(once=True)
    assert actual==[j.id for j in jobs]
