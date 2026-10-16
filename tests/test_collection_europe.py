"""Collection, exact-identity domestic context and source-only quote repair."""
from copy import deepcopy
from datetime import timedelta,datetime
from pathlib import Path
import os,uuid,json,asyncio
import pytest
from sqlalchemy import select,delete,func
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema,DropSchema
from src.db import Database
from src.models import League,Season,Team,Match,ScraperJob,PredictionBoard,CollectorRetry,Odds1X2,Bookmaker,JobItem
from src.jobs.worker import enqueue_due_updates
from src.jobs.monitoring import collection_health
from src.jobs.live import PriorityUpdater
from src.scrapers.goaloo.odds import parse_odds,odds_gaps
from tests.test_predictions import seed,NOW
from tests.test_goaloo import fixture


async def test_fallback_scheduler_deduplicates_and_preserves_running_backfill(db):
    async with db.session() as s:
        leagues=(await s.scalars(select(League))).all();league=leagues[0]
        for l in leagues:l.enabled=l.id==league.id
        old=ScraperJob(kind='backfill',league_id=league.id,status='running',created_at=NOW-timedelta(days=5),processed_matches=19,total_matches=50)
        s.add(old);await s.commit();old_id=old.id;league_id=league.id
    created=await enqueue_due_updates(db,NOW)
    assert len(created)==1 and created[0].kind=='update'
    assert await enqueue_due_updates(db,NOW)==[]
    async with db.session() as s:
        running=await s.get(ScraperJob,old_id)
        assert (running.status,running.processed_matches,running.total_matches)==('running',19,50)
        job=await s.get(ScraperJob,created[0].id)
        job.created_at=NOW;job.status='partial';job.discovery_complete=True;job.discovery_finished_at=NOW
        job.discovery_metrics={'current/1':dict(discovered=100,added=0,updated=7,unchanged=93,errors=0)}
        await s.commit()
        health=await collection_health(s,NOW)
        assert health['stale_fixture_leagues']==0 and health['partial_collection_jobs_24h']==1
        assert health['fixture_collection'][0]['added']==0 and health['fixture_collection'][0]['updated']==7
    assert await enqueue_due_updates(db,NOW+timedelta(hours=23))==[]
    assert len(await enqueue_due_updates(db,NOW+timedelta(hours=25)))==1


async def test_failed_discovery_retry_waits_six_hours(db):
    async with db.session() as s:
        leagues=(await s.scalars(select(League))).all()
        for l in leagues:l.enabled=l.id==leagues[0].id
        s.add(ScraperJob(kind='update',league_id=leagues[0].id,status='partial',created_at=NOW,
            discovery_complete=True,discovery_finished_at=NOW,discovery_metrics={'season/1':dict(errors=1)}))
        await s.commit()
    assert await enqueue_due_updates(db,NOW+timedelta(hours=5))==[]
    assert len(await enqueue_due_updates(db,NOW+timedelta(hours=7)))==1


async def verify_quotes(db,monkeypatch):
    import src.jobs.live as live
    import src.jobs.storage as storage
    now=NOW;monkeypatch.setattr(live,'utcnow',lambda:now);monkeypatch.setattr(storage,'utcnow',lambda:now)
    target,*_=await seed(db)
    async with db.session() as s:
        await s.execute(delete(PredictionBoard));await s.commit()
    payload=fixture('goaloo_odds.json');partial=deepcopy(payload)
    next(r for r in partial['Data']['mixodds'] if int(r['cid'])==31)['euro']={}
    calls=[];complete=False
    async def source(client,external,final):
        assert db.engine.pool.checkedout()==0
        calls.append(external);return parse_odds(payload if complete else partial,final)
    import src.scrapers.goaloo.odds as odds
    monkeypatch.setattr(odds,'fetch_odds',source)
    updater=PriorityUpdater(db)
    await updater.upcoming_quotes(None)
    assert len(calls)==1
    async with db.session() as s:
        m=await s.get(Match,target)
        retry=await s.get(CollectorRetry,f'quotes:full_time:{m.external_match_id}')
        assert retry.reason=='quote_partial_source'
        row=await s.scalar(select(Odds1X2).join(Bookmaker).where(Odds1X2.match_id==target,Bookmaker.name=='Sbobet'))
        # The retained price's observation must not become current on a missing response.
        assert row.raw['_field_observed_at']['latest_home']==(NOW-timedelta(minutes=1)).isoformat()
    await updater.upcoming_quotes(None);assert len(calls)==1
    now+=timedelta(minutes=5,seconds=1);complete=True
    await updater.upcoming_quotes(None);assert len(calls)==2
    async with db.session() as s:
        row=await s.scalar(select(Odds1X2).join(Bookmaker).where(Odds1X2.match_id==target,Bookmaker.name=='Sbobet'))
        assert row.latest_home>1 and row.raw['_field_observed_at']['latest_home']==now.isoformat()


async def test_quotes_repaired_without_qualifying_board_and_with_backoff(db,monkeypatch):
    await verify_quotes(db,monkeypatch)


async def test_real_crown_source_capture_repairs_empty_row_offline(db,monkeypatch):
    """Replay an observed production omission with actual newly available source prices."""
    import src.jobs.live as live
    import src.jobs.storage as storage
    import src.scrapers.goaloo.odds as odds
    payload=json.loads(Path('tests/fixtures/goaloo_3073151_20261015.json').read_text(encoding='utf-8'))
    now=datetime.fromisoformat(payload['_audit']['observed_at'])
    monkeypatch.setattr(live,'utcnow',lambda:now);monkeypatch.setattr(storage,'utcnow',lambda:now)
    target,*_=await seed(db)
    async with db.session() as s:
        await s.execute(delete(PredictionBoard))
        m=await s.get(Match,target);m.external_match_id=3073151;m.kickoff_at=datetime.fromisoformat(payload['_audit']['production_kickoff_at'])
        row=await s.scalar(select(Odds1X2).join(Bookmaker).where(Odds1X2.match_id==target,Bookmaker.name=='Crown'))
        for stage in ('opening','latest'):
            for side in ('home','draw','away'):setattr(row,stage+'_'+side,None)
        row.raw={};await s.commit()
        assert [row.latest_home,row.latest_draw,row.latest_away]==[None,None,None]
    async def source(client,external,final):
        assert external==payload['_audit']['requested_match_id'] and not final
        return parse_odds(payload,final)
    monkeypatch.setattr(odds,'fetch_odds',source)
    await PriorityUpdater(db).upcoming_quotes(None)
    async with db.session() as s:
        row=await s.scalar(select(Odds1X2).join(Bookmaker).where(Odds1X2.match_id==target,Bookmaker.name=='Crown'))
        actual=parse_odds(payload)['Crown']['1x2']
        for side in ('home','draw','away'):
            assert getattr(row,'latest_'+side)==actual['latest_'+side]>1
            assert row.raw['_field_observed_at']['latest_'+side]==now.isoformat()


async def test_empty_source_does_not_freshen_any_stored_price(db,monkeypatch):
    import src.jobs.live as live
    import src.scrapers.goaloo.odds as odds
    monkeypatch.setattr(live,'utcnow',lambda:NOW)
    target,*_=await seed(db)
    async with db.session() as s:
        await s.execute(delete(PredictionBoard));await s.commit()
        before=[deepcopy(r.raw) for r in await s.scalars(select(Odds1X2).where(Odds1X2.match_id==target).order_by(Odds1X2.id))]
    async def source(*args):return parse_odds({'Data':{'mixodds':[]}})
    monkeypatch.setattr(odds,'fetch_odds',source)
    await PriorityUpdater(db).upcoming_quotes(None)
    async with db.session() as s:
        after=[r.raw for r in await s.scalars(select(Odds1X2).where(Odds1X2.match_id==target).order_by(Odds1X2.id))]
        assert after==before
        assert await s.scalar(select(CollectorRetry.failures))==1


async def test_real_opening_survives_missing_latest_without_freshening_it(db,monkeypatch):
    import src.jobs.live as live
    import src.jobs.storage as storage
    import src.scrapers.goaloo.odds as odds
    monkeypatch.setattr(live,'utcnow',lambda:NOW);monkeypatch.setattr(storage,'utcnow',lambda:NOW)
    target,*_=await seed(db)
    async with db.session() as s:await s.execute(delete(PredictionBoard));await s.commit()
    payload=fixture('goaloo_odds.json')
    for book in payload['Data']['mixodds']:
        for market in ('euro','ah','ou'):
            if isinstance(book.get(market),dict):book[market]['l']={}
    async def source(*args):return parse_odds(payload)
    monkeypatch.setattr(odds,'fetch_odds',source)
    await PriorityUpdater(db).upcoming_quotes(None)
    async with db.session() as s:
        row=await s.scalar(select(Odds1X2).join(Bookmaker).where(Odds1X2.match_id==target,Bookmaker.name=='Crown'))
        assert row.opening_home==parse_odds(payload)['Crown']['1x2']['opening_home']
        assert row.raw['_field_observed_at']['opening_home']==NOW.isoformat()
        assert row.raw['_field_observed_at']['latest_home']==(NOW-timedelta(minutes=1)).isoformat()
        retry=await s.scalar(select(CollectorRetry))
        assert retry.reason=='no_current_source_prices' and retry.failures==1


def test_missing_market_details_keep_future_close_separate():
    data=parse_odds(fixture('goaloo_odds.json'),False)
    assert odds_gaps(data)==[]
    assert all(g['stage']=='closing' for g in odds_gaps(data,True))
    data['Crown']['ou']['latest_over']=None
    assert dict(bookmaker='Crown',market='ou',stage='latest',missing=['over']) in odds_gaps(data)


async def verify_domestic(db):
    from src.analytics.domestic_context import domestic_context
    target,league,season,teams,history=await seed(db)
    bad=[]
    async with db.session() as s:
        european=await s.scalar(select(League).where(League.external_id==103))
        if european is None:
            european=League(external_id=103,name='UEFA Champions League',country='Europe',source='goaloo',competition_type='club',schedule_format='cup',enabled=True)
            s.add(european);await s.flush()
        european.enabled=True
        cup_season=Season(league_id=european.id,season_name='2026-2027');s.add(cup_season);await s.flush()
        m=await s.get(Match,target);m.league_id=european.id;m.season_id=cup_season.id
        impostor=Team(external_id=701010,name='Test team 0');s.add(impostor);await s.flush()
        for index,values in enumerate([
            dict(kickoff_at=NOW+timedelta(days=1)),dict(kickoff_at=NOW-timedelta(hours=1)),
            dict(kickoff_at=NOW-timedelta(days=5),updated_at=NOW+timedelta(seconds=1)),
            dict(kickoff_at=NOW-timedelta(days=3),home_team_id=impostor.id,away_team_id=teams[2]),
        ]):
            row=Match(external_match_id=701020+index,league_id=league,season_id=season,round=1,
                home_team_id=teams[0],away_team_id=teams[1],kickoff_at=NOW-timedelta(days=2),
                status='finished',ft_home=99,ft_away=0,created_at=NOW-timedelta(days=10),updated_at=NOW-timedelta(days=1),raw={})
            for k,v in values.items():setattr(row,k,v)
            s.add(row);await s.flush();bad.append(row.id)
        await s.commit()
        result=await domestic_context(s,m,NOW)
        assert result['home']['form']['sample_size']==10
        assert result['home']['source_competitions'][0]['external_id']==36
        assert not set(bad)&set(result['home']['match_ids'])
        assert not result['affects_model_probability'] and not result['confidence_uplift']
    from src.analytics.board_cache import refresh_one
    await refresh_one(db,now=NOW,force_league=european.id)
    return target


async def test_domestic_exact_identity_and_asof_no_future_leakage(api,db,monkeypatch):
    target=await verify_domestic(db)
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert data['domestic_context']['home']['available']
    assert data['prediction']['status']=='insufficient_data'
    assert data['prediction']['league_sample_size']==0


@pytest.mark.parametrize('scenario',['quotes','domestic'])
@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_collection_and_domestic_history(scenario,monkeypatch):
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='collection_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as c:await c.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        if scenario=='quotes':
            await verify_quotes(db,monkeypatch)
            groups=await asyncio.gather(enqueue_due_updates(db,NOW),enqueue_due_updates(db,NOW))
            created=[j for group in groups for j in group]
            async with db.session() as s:
                expected=await s.scalar(select(func.count(League.id)).where(League.enabled.is_(True)))
                assert len(created)==len({j.league_id for j in created})==expected
                health=await collection_health(s,NOW)
                assert health['active_collection_jobs']==expected
        else:await verify_domestic(db)
    finally:
        async with db.engine.begin() as c:await c.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


async def test_chromium_collection_monitor_and_domestic_context(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await verify_domestic(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport=dict(width=1440,height=900))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/')
            await page.locator('#collection-monitor > summary').click()
            await expect(page.locator('#collection-leagues tr')).not_to_have_count(0)
            await expect(page.locator('#collection-failures')).to_contain_text('Son 24 saat')
            await page.evaluate('(id)=>BetAppDashboard.selectMatch(id)',target)
            await expect(page.locator('#analysis-dialog')).to_be_visible()
            await page.locator('[data-analysis-tab="form"]').click()
            await expect(page.locator('#statistics-detail')).to_contain_text('Yerel Lig Formu')
            await expect(page.locator('#statistics-detail')).to_contain_text('English Premier League')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:await page.unroute_all(behavior='ignoreErrors');await browser.close()
