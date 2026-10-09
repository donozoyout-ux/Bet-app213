"""Live collection must not depend on a historical job finishing."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest
from sqlalchemy import select

from src.models import Match, League, Team, Season, MatchStatistics, ScraperJob, JobItem
from src.jobs.live import PriorityUpdater
from src.jobs.storage import store_match, store_live
from src.scrapers.goaloo.live import identity, parse_scoreboard, fetch_live
from src.scrapers.goaloo.client import SourceError

NOW=datetime(2026,10,9,17,40,tzinfo=timezone.utc)
SOURCE=Path('tests/fixtures/live_3026745.html').read_text(encoding='utf-8')
SCORE=json.loads(Path('tests/fixtures/live_3026745.json').read_text(encoding='utf-8'))


async def seed_match(db):
    async with db.session() as session:
        league=await session.scalar(select(League).where(League.external_id==30))
        if not league:
            league=League(external_id=30,name='Turkey Super Lig',country='Turkey',source='goaloo',enabled=True)
            session.add(league);await session.flush()
        season=Season(league_id=league.id,season_name='2026-2027')
        home=Team(external_id=516,name='Galatasaray');away=Team(external_id=4469,name='Kasimpasa')
        session.add_all([season,home,away]);await session.flush()
        match=Match(id=5959,external_match_id=3026745,league_id=league.id,season_id=season.id,
            round=8,home_team_id=home.id,away_team_id=away.id,kickoff_at=NOW-timedelta(minutes=40),
            status='scheduled',last_scraped_at=NOW-timedelta(days=1),raw={})
        job=ScraperJob(league_id=league.id,kind='stats_backfill',status='running',total_matches=829,processed_matches=354)
        session.add_all([match,job]);await session.commit()
        return league.id,job.id


class Source:
    def __init__(self):self.payload=deepcopy(SCORE);self.calls=0
    async def get_text(self,url):
        self.calls+=1
        return SOURCE.replace("state: parseInt('1')",f"state: parseInt('{self.payload['Data']['state']}')")
    async def get(self,url,params):
        assert params=={'type':11,'id':3026745}
        return self.payload


def test_verified_galatasaray_source_and_no_invented_minutes():
    assert identity(SOURCE,3026745,30,516,4469)['state']==1
    result=parse_scoreboard(SCORE)
    assert (result['status'],result['ft_home'],result['ft_away'],result['live_minute'])==('live',0,0,None)
    with pytest.raises(SourceError):identity(SOURCE,3026745,31,516,4469)
    with pytest.raises(SourceError):identity(SOURCE,3026745,30,4469,516)


@pytest.mark.parametrize('clock,expected',[('45+2', '45+2'),("67′",'67'),('Ongoing',None),('HT',None),('',None)])
def test_only_explicit_source_minutes(clock,expected):
    payload=deepcopy(SCORE);payload['Data']['html']=payload['Data']['html'].replace('Ongoing',clock)
    assert parse_scoreboard(payload)['live_minute']==expected


async def test_scheduled_live_finished_while_backfill_active(api,db,monkeypatch):
    import src.jobs.live as live
    league,job_id=await seed_match(db)
    monkeypatch.setattr(live,'utcnow',lambda:NOW)
    updater=PriorityUpdater(db);source=Source()
    # Kickoff alone cannot put a scheduled fixture in the live view.
    assert (await api.get('/api/matches?view=live')).json()['total']==0
    await updater.live_cycle(source)
    rows=(await api.get('/api/matches?view=live')).json()['items']
    assert len(rows)==1 and rows[0]['id']==5959 and rows[0]['ft_home']==0 and rows[0]['ft_away']==0
    assert rows[0]['live_minute'] is None
    async with db.session() as session:
        stats=await session.get(MatchStatistics,5959)
        assert (stats.home_corners,stats.away_corners,stats.home_yellow_cards,stats.home_red_cards)==(3,0,1,None)
        job=await session.get(ScraperJob,job_id)
        assert (job.status,job.processed_matches,job.total_matches)==('running',354,829)
    await updater.live_cycle(source)
    assert source.calls==1  # Persistent 60-second cadence.
    monkeypatch.setattr(live,'utcnow',lambda:NOW+timedelta(seconds=61))
    source.payload['Data']['state']=-1
    source.payload['Data']['html']="<div class='end'><div class='score'>2</div><div>Finished</div><div class='score'>1</div></div>"
    await updater.live_cycle(source)
    assert (await api.get('/api/matches?view=live')).json()['total']==0
    detail=(await api.get('/api/matches/5959')).json()
    assert (detail['status'],detail['ft_home'],detail['ft_away'])==('finished',2,1)
    monkeypatch.setattr(live,'utcnow',lambda:NOW+timedelta(hours=1))
    await updater.live_cycle(source)
    assert source.calls==2  # Final matches leave the intensive live cycle.
    async with db.session() as session:
        assert (await session.get(MatchStatistics,5959)).home_red_cards is None


async def test_live_score_survives_statistics_parser_failure(db,monkeypatch):
    import src.jobs.live as live
    await seed_match(db);monkeypatch.setattr(live,'utcnow',lambda:NOW)
    def fail(*args):raise SourceError('changed stats markup')
    monkeypatch.setattr(live,'parse_statistics',fail)
    await PriorityUpdater(db).live_cycle(Source())
    async with db.session() as session:
        match=await session.get(Match,5959)
        assert match.status=='live' and match.ft_home==0


async def test_disabled_league_is_not_polled(db,monkeypatch):
    import src.jobs.live as live
    league,_=await seed_match(db);monkeypatch.setattr(live,'utcnow',lambda:NOW)
    async with db.session() as session:
        (await session.get(League,league)).enabled=False
        await session.commit()
    source=Source();await PriorityUpdater(db).live_cycle(source)
    assert source.calls==0


async def test_past_kickoff_stays_scheduled_when_source_says_scheduled(db,monkeypatch):
    import src.jobs.live as live
    await seed_match(db);monkeypatch.setattr(live,'utcnow',lambda:NOW)
    source=Source();source.payload['Data']['state']=0
    await PriorityUpdater(db).live_cycle(source)
    async with db.session() as session:
        match=await session.get(Match,5959)
        assert match.status=='scheduled' and match.live_minute is None
        assert match.ft_home is None and match.ft_away is None


async def test_live_fetch_does_not_hold_transaction_and_api_remains_responsive(api,db,monkeypatch):
    import src.jobs.live as live
    await seed_match(db);monkeypatch.setattr(live,'utcnow',lambda:NOW)
    entered,release=asyncio.Event(),asyncio.Event()
    async def waiting(*args):
        assert db.engine.pool.checkedout()==0
        entered.set();await release.wait()
        return parse_scoreboard(SCORE),SOURCE
    monkeypatch.setattr(live,'fetch_live',waiting)
    task=asyncio.create_task(PriorityUpdater(db).live_cycle(None))
    try:
        await asyncio.wait_for(entered.wait(),2)
        results=await asyncio.wait_for(asyncio.gather(*[api.get(p) for p in ['/health','/api/status','/api/matches?view=live','/api/diagnostics']]),2)
        assert all(r.status_code==200 for r in results)
    finally:
        release.set();await task


async def test_update_enqueue_not_suppressed_by_active_statistics(db,monkeypatch):
    import src.jobs.worker as worker
    league,job_id=await seed_match(db);monkeypatch.setattr(worker,'database',db)
    jobs=await worker.enqueue_all_updates()
    assert all(j.kind=='update' for j in jobs)
    assert next(j for j in jobs if j.league_id==league).id!=job_id
    assert [j.id for j in await worker.enqueue_all_updates()]==[j.id for j in jobs]
    # Explicit league update must also coexist with a historical job.
    async with db.session() as session:
        for j in jobs:(await session.get(ScraperJob,j.id)).status='completed'
        await session.commit()
    assert (await worker.enqueue('update',30)).kind=='update'


async def test_historical_job_yields_to_update_without_losing_checkpoint(db,monkeypatch):
    import src.jobs.worker as worker
    league,job_id=await seed_match(db);monkeypatch.setattr(worker,'database',db)
    async with db.session() as session:
        job=await session.get(ScraperJob,job_id);job.discovery_complete=True
        session.add(JobItem(job_id=job_id,match_id=5959,status='queued'))
        await session.commit()
    update=await worker.enqueue('update',30)
    async def forbidden(*args):raise AssertionError('Historical source should yield to update')
    monkeypatch.setattr(worker,'process_item',forbidden)
    await worker.run_job(job_id)
    async with db.session() as session:
        assert (await session.get(ScraperJob,job_id)).processed_matches==354
        assert (await session.get(ScraperJob,job_id)).status=='running'
    seen=[]
    async def run(id):seen.append(id)
    monkeypatch.setattr(worker,'run_job',run)
    await worker.work(once=True)
    assert seen==[update.id]


async def test_stale_schedule_cannot_undo_live_status(db):
    league,_=await seed_match(db)
    async with db.session() as session:
        match=await session.get(Match,5959)
        await store_live(session,match,parse_scoreboard(SCORE),NOW)
        await session.commit()
        stale=dict(external_match_id=3026745,round=8,kickoff_at=NOW-timedelta(minutes=40),status='scheduled',
            home_external_id=516,away_external_id=4469,home_name='Galatasaray',away_name='Kasimpasa',
            ht_home=None,ht_away=None,ft_home=None,ft_away=None,raw={})
        await store_match(session,league,'2026-2027',stale);await session.commit()
        assert match.status=='live' and match.ft_home==0


async def test_recent_statistics_rotates_all_eight_leagues_and_reports_progress(api,db,monkeypatch):
    import src.jobs.live as live
    from src.scrapers.goaloo.competitions import upsert_competitions,verified_snapshot,apply_production_scope,PRODUCTION_LEAGUE_IDS
    monkeypatch.setattr(live,'utcnow',lambda:NOW)
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot());await apply_production_scope(session)
        session.add_all([Team(external_id=516,name='Test home'),Team(external_id=4469,name='Test away')]);await session.flush()
        teams=(await session.scalars(select(Team).order_by(Team.id))).all()
        leagues=(await session.scalars(select(League).where(League.enabled.is_(True)).order_by(League.priority))).all()
        for i,l in enumerate(leagues):
            season=Season(league_id=l.id,season_name='2026-2027');session.add(season);await session.flush()
            session.add(Match(external_match_id=4000000+i,league_id=l.id,season_id=season.id,round=1,
                home_team_id=teams[0].id,away_team_id=teams[1].id,status='finished',kickoff_at=NOW-timedelta(days=2),raw={}))
            session.add(ScraperJob(league_id=l.id,kind='stats_backfill',status='queued',total_matches=100,processed_matches=i))
        await session.commit()
    calls=[]
    async def stats(client,id,league):
        calls.append(league)
        return dict(statistics={'home_corners':3,'away_corners':0,'home_yellow_cards':1,'away_yellow_cards':1},
            is_final=True,events_available=False,events=[],referee=None,raw={'match_id':id})
    monkeypatch.setattr(live,'fetch_statistics',stats)
    updater=PriorityUpdater(db)
    for _ in range(8):await updater.recent_statistics(None)
    assert calls==list(PRODUCTION_LEAGUE_IDS)
    for _ in range(8):await updater.recent_statistics(None)
    assert len(calls)==8  # Partial data gets a cooldown, not a tight retry loop.
    rows=(await api.get('/api/diagnostics')).json()['stats_coverage_by_league']
    assert len(rows)==8 and all(r['matches_with_corners']==1 and r['matches_with_cards']==0 for r in rows)
    assert [r['stats_backfill']['processed'] for r in rows]==list(range(8))


import os
@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_live_update_and_concurrent_http_with_active_backfill(monkeypatch):
    import uuid
    from httpx import AsyncClient,ASGITransport
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    from src.api.main import app
    import src.api.routes as routes
    db=Database(os.environ['TEST_POSTGRES_URL'])
    schema='betapp_live_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        monkeypatch.setattr(routes,'database',db)
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as api:
            await test_live_fetch_does_not_hold_transaction_and_api_remains_responsive(api,db,monkeypatch)
            # Read the actual stored transition, not the route's mocked response.
            result=(await api.get('/api/matches?view=live')).json()
            assert result['items'][0]['id']==5959 and result['items'][0]['ft_home']==0
        async with db.session() as session:
            job=await session.scalar(select(ScraperJob).where(ScraperJob.kind=='stats_backfill'))
            assert (job.status,job.processed_matches)==('running',354)
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
