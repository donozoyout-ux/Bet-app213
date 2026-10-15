"""Coverage evidence, chronology and offline challenger admission tests."""
from copy import deepcopy
from datetime import timedelta
import gzip
import json
import os
from pathlib import Path
import random
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from src.analytics.uefa_coverage import usable, challenger, metrics, team_coverage, report
from src.analytics.domestic_context import uefa_team_coverage
from src.jobs.uefa_backfill import enqueue_domestic_backfill
from src.models import Match, League, ScraperJob
from tests.test_predictions import NOW
from tests.test_collection_europe import verify_domestic


def row(i,h,a,league=1,score=(2,1)):
    when=NOW-timedelta(days=300-i)
    return dict(id=i,league_id=league,home_team_id=h,away_team_id=a,status='finished',
        ft_home=score[0],ft_away=score[1],kickoff_at=when,created_at=when,
        updated_at=when+timedelta(hours=3),last_scraped_at=None)


def evidence():
    leagues={i:dict(id=i,external_id=113 if i==3 else i,name=str(i),enabled=True,
        schedule_format='cup' if i==3 else 'league') for i in (1,2,3)}
    rows=[]
    for i in range(60):
        rows.append(row(i*2,1 if i%2 else 2,2 if i%2 else 1,1))
        rows.append(row(i*2+1,3 if i%2 else 4,4 if i%2 else 3,2))
    for i in range(60):rows.append(row(120+i,1 if i%2 else 3,3 if i%2 else 1,3))
    return leagues,rows,dict(league_id=3,home_team_id=1,away_team_id=3)


def test_challenger_is_opponent_adjusted_not_goal_average_pooling():
    leagues,rows,target=evidence(); p=challenger(rows,target,leagues)
    assert p['status']=='ok'
    assert sum(p[k+'_probability'] for k in ('home','draw','away'))==pytest.approx(1)
    inflated=deepcopy(rows)
    for r in inflated:r['ft_home']*=100;r['ft_away']*=100
    assert p==challenger(inflated,target,leagues)
    reversed_results=deepcopy(rows)
    for r in reversed_results:
        if r['league_id']==3:r['ft_home'],r['ft_away']=r['ft_away'],r['ft_home']
    assert challenger(reversed_results,target,leagues)!=p
    random.Random(19).shuffle(rows)
    assert challenger(rows,target,leagues)==p
    assert challenger([r for r in rows if r['league_id']!=3],target,leagues)['status']=='insufficient_data'


def test_stamps_and_buffer_exclude_future_backfills_and_corrections():
    r=row(1,1,2);assert usable(r,NOW)
    for field in ('created_at','updated_at','last_scraped_at'):
        changed={**r,field:NOW+timedelta(seconds=1)};assert not usable(changed,NOW)
    assert not usable({**r,'kickoff_at':NOW-timedelta(hours=2)},NOW)
    assert not usable({**r,'ft_home':None},NOW)
    assert not usable({**r,'ft_home':-1},NOW)
    assert not usable({**r,'created_at':None},NOW)


def test_metrics_are_multiclass_per_match_and_empty_is_not_zero():
    assert metrics([])['brier'] is None
    result=metrics([([.5,.25,.25],0)])
    assert result['brier']==pytest.approx(.375)
    assert result['log_loss']==pytest.approx(__import__('math').log(2))
    assert result['matches']==1


async def verify_coverage(db):
    target=await verify_domestic(db)
    async with db.session() as s:
        m=await s.get(Match,target)
        c=await uefa_team_coverage(s,m,NOW)
        assert c['home']['europe_matches']==0
        assert c['home']['domestic_matches']==80
        assert c['home']['domestic_home']==40
        assert c['away']['domestic_away']==40
        league=await s.scalar(select(League).where(League.external_id==103))
        league.external_id=113
        domestic=await s.get(League,c['home']['domestic_leagues'][0]['league_id'])
        domestic.enabled=True;domestic.verified_at=NOW
        await s.commit()
    first=await enqueue_domestic_backfill(db,NOW)
    assert len(first)==1 and first[0]['status']=='queued'
    second=await enqueue_domestic_backfill(db,NOW)
    assert second[0]['status']=='active_or_cooldown'
    async with db.session() as s:
        job=await s.get(ScraperJob,first[0]['job_id'])
        assert job.kind=='domestic_history' and job.start_year==2024


async def test_coverage_and_backfill_deduplication(db):
    await verify_coverage(db)


async def test_archive_backfill_only_inserts_missing_results_without_odds_or_rewrites(db,monkeypatch):
    from tests.test_goaloo import fixture
    import src.jobs.worker as worker
    from src.jobs.worker import discover
    from src.scrapers.goaloo.matches import parse_matches
    from src.jobs.storage import store_match
    from src.models import JobItem
    monkeypatch.setattr(worker,'database',db)
    payload=fixture('goaloo_league.json')
    async def seasons(*args,**kwargs):
        assert kwargs['overlap'] is True
        return ['2024-2025']
    async def data(*args):return payload
    async def priority(*args):pass
    monkeypatch.setattr(worker,'discover_seasons',seasons)
    monkeypatch.setattr(worker,'season_data',data)
    monkeypatch.setattr(worker,'priority_cycle',priority)
    async with db.session() as s:
        league=await s.scalar(select(League).where(League.external_id==36))
        existing=await store_match(s,league.id,'2024-2025',next(parse_matches(payload,1)))
        existing.ft_home=17;existing.updated_at=NOW-timedelta(days=10)
        job=ScraperJob(kind='domestic_history',league_id=league.id,start_year=2024)
        s.add(job);await s.commit();job_id=job.id;match_id=existing.id
    await discover(job_id,None)
    async with db.session() as s:
        m=await s.get(Match,match_id)
        assert m.ft_home==17
        assert not await s.scalar(select(JobItem.id).where(JobItem.job_id==job_id))
        assert len((await s.scalars(select(Match))).all())==10
        assert (await s.get(ScraperJob,job_id)).discovery_complete
    await discover(job_id,None)
    async with db.session() as s:assert len((await s.scalars(select(Match))).all())==10


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_uefa_coverage_and_backfill():
    from src.db import Database
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='uefa_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as c:await c.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await verify_coverage(db)
    finally:
        async with db.engine.begin() as c:await c.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


@pytest.mark.parametrize('width',[390,768,1440])
async def test_chromium_uefa_counts_and_safe_text(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await verify_domestic(db)
    async with db.session() as s:
        from src.models import Team
        m=await s.get(Match,target);team=await s.get(Team,m.home_team_id)
        team.name='<img src=x onerror="window.coverageXss=1">'
        await s.commit()
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport=dict(width=width,height=900))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/')
            await page.evaluate('(id)=>BetAppDashboard.selectMatch(id)',target)
            await expect(page.locator('.uefa-coverage')).to_contain_text('Avrupa: 0')
            await expect(page.locator('.uefa-coverage')).to_contain_text('Yerel lig: 80')
            await expect(page.locator('.uefa-coverage')).to_contain_text('<img src=x')
            assert await page.locator('.uefa-coverage img').count()==0
            assert await page.evaluate('window.coverageXss===undefined')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


def test_audited_coverage_report_reproducible():
    path=Path(__file__).parents[1]/'reports/uefa-coverage'
    source=json.loads(gzip.decompress((path/'source.json.gz').read_bytes()))
    expected=json.loads((path/'report.json').read_text(encoding='utf-8'))
    assert report(source)==expected
    assert expected['upcoming_matches']==36
    assert expected['current_analyzable']==expected['actual_after_analyzable']==12
    assert expected['live_model_changed'] is False
