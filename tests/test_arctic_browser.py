"""Arctic shell browser contracts backed by FastAPI and isolated test records."""
import asyncio
from datetime import timedelta
import pytest
from playwright.async_api import async_playwright, expect
from src.models import Match, MatchStatistics
from tests.test_predictions import seed, NOW


@pytest.mark.parametrize('width',[1440,768,390])
async def test_arctic_navigation_filters_analysis_and_responsive_layout(api,db,monkeypatch,width):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,league,season,teams,history=await seed(db)
    async with db.session() as session:
        second=Match(external_match_id=999998,league_id=league,season_id=season,round=2,
            home_team_id=teams[1],away_team_id=teams[0],status='scheduled',kickoff_at=NOW+timedelta(days=2),raw={})
        session.add(second)
        for id in history:session.add(MatchStatistics(match_id=id,is_final=True,home_corners=2,away_corners=2,
            home_yellow_cards=1,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit();second_id=second.id
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':900},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        calls=[];release=asyncio.Event();mode='normal'
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            if path.startswith('/api/predictions?'):await release.wait()
            if mode=='timeout' and path.startswith('/api/predictions/best'):
                await route.fulfill(status=504,body='Timeout');return
            if mode=='db-down' and path.startswith('/api/predictions/best'):
                await route.fulfill(status=503,body='Unavailable');return
            if mode=='empty' and path.startswith('/api/predictions/best'):
                await route.fulfill(json={'items':[],'evaluated_matches':0,'generated_at':NOW.isoformat(),'availability':{'status':'no_upcoming_matches'}});return
            response=await api.get(path)
            if path=='/api/diagnostics':
                data=response.json();data['active_stats_backfill']={'league':'English Premier League','processed':17,'failed':1,'total':80};data['queued_stats_backfills']=[{'id':2}]
                data['stats_coverage_by_league'][0]['stats_backfill']={'status':'running','processed':17,'failed':1,'total':80}
                await route.fulfill(json=data);return
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/')
            await expect(page.locator('#prediction-cards')).to_have_attribute('aria-busy','true')
            assert not any('/statistics' in path or '/market-performance' in path or '/diagnostics' in path for path in calls)
            release.set()
            await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(2)
            columns=await page.locator('#prediction-cards').evaluate('(el)=>getComputedStyle(el).gridTemplateColumns.split(" ").length')
            assert columns=={1440:3,768:2,390:1}[width]
            assert await page.locator('.prediction-card').first.evaluate('(el)=>getComputedStyle(el).transitionDuration')=='0s'
            nav='.bottom-nav' if width==390 else '.desktop-nav'
            async def go(name):
                await page.locator(f'{nav} [data-page-link="{name}"]').click()
                await expect(page.locator('[data-page]:visible')).to_have_count(1)
                await expect(page.locator(f'[data-page="{name}"]')).to_be_visible()
                assert page.url.endswith('#'+name)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await go('predictions')
            await expect(page.locator('#market-picks tr[data-match-id]')).not_to_have_count(0)
            await page.locator('#pred-market').select_option('cards')
            await expect(page.locator('#market-picks')).to_contain_text('Kart')
            await page.locator('#pred-league').select_option(str(league))
            await page.locator('#pred-team').fill('Test team 0')
            await expect(page.locator('#market-picks')).to_contain_text('Test team 0')
            await page.wait_for_function('document.getElementById("market-picks").getAttribute("aria-busy")==="false"')
            assert any('league='+str(league) in path and 'market=cards' in path for path in calls)
            # Keyboard opens the exact selected fixture, with all eight relevant tabs.
            row=page.locator(f'#market-picks tr[data-match-id="{target}"]');await row.focus();await page.keyboard.press('Enter')
            await expect(page.locator('#analysis-dialog')).to_be_visible()
            await expect(page.locator('#detail-title')).to_have_text('Test team 0 vs Test team 1')
            assert '?match='+str(target) in page.url
            await expect(page.locator('[data-analysis-tab]')).to_have_count(8)
            for tab in ['summary','predictions','goals','corners','cards','handicap','odds','form']:
                await page.locator(f'[data-analysis-tab="{tab}"]').click()
                await expect(page.locator(f'[data-analysis-tab="{tab}"]')).to_have_attribute('aria-selected','true')
            await page.locator('[data-analysis-tab="summary"]').focus();await page.keyboard.press('ArrowRight')
            await expect(page.locator('[data-analysis-tab="predictions"]')).to_be_focused()
            if width==390:assert await page.locator('#analysis-dialog').evaluate('(el)=>Math.abs(el.getBoundingClientRect().width-innerWidth)<2')
            await page.go_back();await expect(page.locator('#analysis-dialog')).not_to_be_visible()
            await page.go_forward();await expect(page.locator('#analysis-dialog')).to_be_visible()
            await page.keyboard.press('Escape');await expect(page.locator('#analysis-dialog')).not_to_be_visible()
            await page.locator(f'#market-picks tr[data-match-id="{second_id}"]').click()
            await expect(page.locator('#detail-title')).to_have_text('Test team 1 vs Test team 0')
            await page.locator('#analysis-close').click();await expect(page.locator('#analysis-dialog')).not_to_be_visible()
            await go('markets');await page.locator('[data-market-family="corners"]').click()
            await expect(page.locator('#stats-league-coverage')).to_contain_text('80 / 80')
            await expect(page.locator('#stats-league-coverage')).to_contain_text('17/80')
            await expect(page.locator('#active-backfill-progress')).to_contain_text('18 / 80')
            await page.locator('[data-market-family="corners"]').focus();await page.keyboard.press('ArrowRight')
            await expect(page.locator('[data-market-family="cards"]')).to_have_attribute('aria-selected','true')
            await go('matches')
            await expect(page.locator('#matches-body tr[data-match-id]')).to_have_count(2)
            await page.locator('[data-view="live"]').click();await expect(page.locator('#matches-body')).to_contain_text('canlı maç yok')
            await page.locator('[data-view="today"]').focus();await page.keyboard.press('ArrowRight')
            await expect(page.locator('[data-view="upcoming"]')).to_have_attribute('aria-selected','true')
            await go('performance');await expect(page.locator('#daily-summary .summary-card')).to_have_count(5)
            await go('home');await page.go_back();await expect(page.locator('[data-page="performance"]')).to_be_visible()
            await page.go_forward();await expect(page.locator('[data-page="home"]')).to_be_visible()
            mode='empty';await go('predictions');await expect(page.locator('#market-picks')).to_contain_text('yaklaşan maç yok')
            mode='db-down';await page.locator('#pred-market').select_option('corners');await expect(page.locator('#market-picks')).to_contain_text('PostgreSQL')
            mode='timeout';await page.locator('#pred-market').select_option('goals');await expect(page.locator('#market-picks')).to_contain_text('zaman aşımına')
            await expect(page.locator('#market-picks')).to_have_attribute('aria-busy','false')
            assert not errors
        finally:
            release.set();await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_duplicate_refresh_and_hidden_tab_do_not_create_polling_storm(api):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page()
        calls=[];entered=asyncio.Event();release=asyncio.Event()
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            if path.startswith('/api/predictions/best'):
                entered.set();await release.wait()
                await route.fulfill(status=504,json={'code':'api_timeout'});return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.clock.install()
            await page.goto('http://test/#predictions')
            await asyncio.wait_for(entered.wait(),5)
            await page.evaluate('BetAppMarket.refresh(); BetAppMarket.refresh(); BetAppMarket.refresh()')
            assert sum(path.startswith('/api/predictions/best') for path in calls)==1
            await page.evaluate("Object.defineProperty(document,'hidden',{get:()=>true,configurable:true})")
            # Finish outstanding requests before counting hidden-tab polling.
            release.set()
            await expect(page.locator('#market-picks')).to_contain_text('zaman aşımına')
            await page.wait_for_load_state('networkidle')
            before=len(calls)
            await page.clock.run_for(31000)
            assert len(calls)==before
            await expect(page.locator('#market-picks')).to_have_attribute('aria-busy','false')
        finally:
            release.set();await page.unroute_all(behavior='ignoreErrors');await browser.close()
