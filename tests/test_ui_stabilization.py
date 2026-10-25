"""Synthetic read-only UI cohorts. No production writes or messaging."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
from types import SimpleNamespace
import asyncio
import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import select, insert, event
from src.models import Team, Season, Match, League, PredictionSnapshot
from src.analytics.performance_trends import build_trends
from src.analytics.chart_diagnostics import chart_diagnostics

NOW = datetime(2026,10,25,8,tzinfo=timezone.utc)


async def captures(db, fixtures=12000, count=97):
    async with db.session() as s:
        league = await s.scalar(select(League.id).limit(1))
        teams=[Team(external_id=810000+i,name=f'Synthetic UI team {i}') for i in range(2)]
        s.add_all(teams);await s.flush()
        season=Season(league_id=league,season_name='2026-2027');s.add(season);await s.flush()
        rows=[dict(external_match_id=800000+i,league_id=league,season_id=season.id,round=1,
            home_team_id=teams[0].id,away_team_id=teams[1].id,
            kickoff_at=NOW+timedelta(hours=i+1) if i<count else NOW-timedelta(days=2),
            status='scheduled' if i<count else 'finished',ft_home=None if i<count else 2,
            ft_away=None if i<count else 1,created_at=NOW-timedelta(days=1),updated_at=NOW,raw={}) for i in range(fixtures)]
        for start in range(0,fixtures,500):await s.execute(insert(Match),rows[start:start+500])
        ids=list(await s.scalars(select(Match.id).where(Match.status=='scheduled').order_by(Match.id)))
        for id in ids:
            s.add(PredictionSnapshot(match_id=id,league_id=league,captured_at=NOW-timedelta(hours=1),
                model_version='synthetic-ui',picks=[dict(id='home',market='result',selection='home',probability=.7)]))
        await s.commit()


async def test_12000_fixtures_are_not_97_picks_or_settlements(api,db):
    await captures(db)
    statements=[]
    def trace(conn,cursor,sql,params,ctx,many):statements.append(sql)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:
        async with db.session() as s:report=await chart_diagnostics(s,NOW)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',trace)
    assert report['matches']==dict(total=12000,finished=11903)
    assert report['snapshots']['prematch']==97
    assert report['outcomes']['saved_picks']==report['outcomes']['pending']==97
    assert report['outcomes']['settled']==0 and report['snapshots']['finished']==0
    assert report['today_and_future_picks']==97
    assert all(c['settled']==c['saved_picks']==0 for c in report['cohorts'].values())
    assert report['settlement_worker']['heartbeat']=='not_instrumented'
    assert report['explanation']=='no_finished_captured_matches'
    assert len(statements)==3 and all(q.lstrip().startswith('SELECT') for q in statements)
    assert any('LIMIT' in q for q in statements)
    assert (await api.get('/api/chart-diagnostics')).status_code==200


async def test_diagnostic_frozen_results_and_bounded_truncation(db,monkeypatch):
    from tests.test_daily_results_chart import history
    await history(db)
    async with db.session() as s:
        data=await chart_diagnostics(s,datetime(2026,10,11,12,tzinfo=timezone.utc))
    assert data['outcomes']['won']==1 and data['outcomes']['lost']==2
    assert data['outcomes']['disputed']==1
    assert data['snapshots']['invalid_excluded']==1
    assert data['snapshots']['duplicates_excluded']==1
    assert data['settlement_worker']['persisted_decisions']>0
    import src.analytics.chart_diagnostics as module
    monkeypatch.setattr(module,'MAX_SNAPSHOTS',1)
    async with db.session() as s:data=await module.chart_diagnostics(s,NOW)
    assert data['truncated'] and data['sample_limit']==data['snapshots']['examined']==1


async def test_diagnostics_cache_single_flight():
    from src.api.read_cache import ReadCache
    class Engine: dialect=SimpleNamespace(name='postgresql')
    engine=Engine();cache=ReadCache(ttl=60);calls=[]
    async def compute():calls.append(1);await asyncio.sleep(.01);return {'total':12000}
    assert await asyncio.gather(*[cache.get(engine,'same',compute) for _ in range(8)])==[{'total':12000}]*8
    assert len(calls)==1


@pytest.mark.parametrize('width',[390,1536])
async def test_ready_lessons_with_saved_picks_and_no_verified_sample(api,db,width):
    await captures(db,fixtures=3,count=2)
    from src.analytics.learning import refresh
    await refresh(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            assert route.request.method=='GET'
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#lessons')
            await expect(page.locator('#learning-next-step')).to_contain_text('Henüz doğrulanmış sonuç örneklemi yok')
            await expect(page.locator('#learning-summary')).to_contain_text('Gözlem aşaması')
            await expect(page.locator('#learning-chart-coverage')).to_contain_text('2 bekleyen')
            await expect(page.locator('#learning-lessons')).to_contain_text('doğrulanmış bir ders değildir')
            assert not errors
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


def trends(mode):
    rows=[]
    n=1 if mode=='one' else 2 if mode=='two' else 80 if mode=='many' else 0
    for i in range(n):
        rows.append(dict(day=(NOW-timedelta(days=1+i%14)).date(),match_id=i+1,
            league='Synthetic',market='result',model_version='synthetic-ui',probability=.7,
            outcome='won' if i%2==0 else 'lost'))
    return dict(build_trends(rows,NOW.date()),checked_at=NOW.isoformat())


def diagnostic(mode):
    n=1 if mode=='one' else 2 if mode=='two' else 80 if mode=='many' else 0
    return dict(checked_at=NOW.isoformat(),truncated=False,sample_limit=5000,
        matches=dict(total=0 if mode=='two' else 12000),snapshots=dict(prematch=97),
        outcomes=dict(saved_picks=97+n,settled=n,pending=97,awaiting_data=0,disputed=0,void=0),
        today_and_future_picks=97)


@pytest.mark.parametrize('width',[390,1536])
@pytest.mark.parametrize('mode',['empty','one','two','many','outage','malformed'])
async def test_chart_visibility_navigation_errors_and_screenshots(api,monkeypatch,width,mode):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960},reduced_motion='reduce')
        await page.clock.install(time=NOW)
        errors=[];calls=[];fail=mode=='outage'
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            assert route.request.method=='GET'
            if path.startswith('/api/performance-trends'):
                await route.fulfill(status=503 if fail else 200,content_type='application/json',
                    body=json.dumps({'daily':'malformed'} if mode=='malformed' else trends(mode),default=str));return
            if path.startswith('/api/chart-diagnostics'):
                await route.fulfill(content_type='application/json',body=json.dumps(diagnostic(mode)));return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/')
            await expect(page.locator('#home-results-coverage')).to_contain_text('97 bekleyen')
            home=page.locator('#home-results-chart')
            if mode in ('outage','malformed'):
                await expect(home).to_contain_text('Veri bağlantısı bekleniyor')
                await expect(page.locator('#home-results-settled')).to_have_text('—')
                await expect(home.locator('svg')).to_have_count(0)
            else:
                await expect(home.locator('svg')).to_be_visible()
                await expect(home.locator('path[data-series]')).to_have_count(1 if mode=='empty' else 3)
                if mode=='empty':
                    await expect(home).to_contain_text('0 doğrulanmış sonuç')
                    await expect(page.locator('#home-results-rate')).to_have_text('—')
                await page.locator('#home-results-window').select_option('30')
                await expect(page.locator('#home-results-daily tr')).to_have_count(30)
                await expect(page.locator('#home-results-daily tr').first.locator('td')).to_have_count(10)
                await expect(page.locator('#home-results-daily tr').first.locator('td').nth(1)).to_have_text('0')
            root=Path(os.environ.get('UI_132_SCREENSHOTS','docs/ui-132'))
            if os.getenv('UI_132_SCREENSHOTS') and mode=='empty':
                root.mkdir(parents=True,exist_ok=True)
                await page.add_style_tag(content='.terminal-header,.bottom-nav,.skip-link,.floating-tools{visibility:hidden!important}')
                await page.locator('#home-daily-results').screenshot(path=str(root/f'after-home-{width}.png'))
            # Hash navigation must initialize the second chart without a reload.
            await page.evaluate("location.hash='performance'")
            chart=page.locator('#trend-chart')
            if mode in ('outage','malformed'):
                await expect(page.locator('#trend-status')).to_contain_text('alınamadı')
                await expect(chart.locator('svg')).to_have_count(0)
            else:
                await expect(chart.locator('svg')).to_be_visible()
                assert (await chart.locator('svg').bounding_box())['height']==(200 if width==390 else 240)
                if mode=='empty':
                    await expect(chart).to_contain_text('0 doğrulanmış sonuç')
                    await expect(chart.locator('circle')).to_have_count(0)
                    await page.locator('#trend-window').select_option('30')
                    await expect(chart.locator('svg')).to_be_visible()
                else:await expect(chart.locator('circle').first).to_be_visible()
                if os.getenv('UI_132_SCREENSHOTS') and mode=='empty':
                    await page.locator('.trend-panel').screenshot(path=str(root/f'after-performance-{width}.png'))
                # A subsequent outage clears data. Resizing cannot resurrect old SVG.
                fail=True
                await page.locator('#trend-as-of').fill('2026-10-24')
                await page.locator('#trend-as-of').dispatch_event('change')
                await expect(page.locator('#trend-status')).to_contain_text('alınamadı')
                await page.set_viewport_size({'width':width,'height':900})
                await expect(chart.locator('svg')).to_have_count(0)
            await page.evaluate("location.hash='lessons'")
            await expect(page.locator('#learning-next-step')).to_contain_text('İlk rapor')
            await expect(page.locator('#learning-lessons')).to_contain_text('Henüz raporlanmış ders yok')
            await expect(page.locator('#learning-chart-coverage')).to_contain_text('97 bekleyen')
            if os.getenv('UI_132_SCREENSHOTS') and mode=='empty':
                await page.locator('[data-page=lessons]').screenshot(path=str(root/f'after-lessons-{width}.png'))
            # Shared diagnostics are fetched once across all three pages within TTL.
            assert sum(path.startswith('/api/chart-diagnostics') for path in calls)==1
            assert not any('telegram' in path or 'nvidia' in path for path in calls)
            assert not errors
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
