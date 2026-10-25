"""Chromium/ASGI contracts; all test records are isolated from production."""
import asyncio
from datetime import timedelta
from copy import deepcopy

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import select, func
from src.models import Match, MatchStatistics, SavedCoupon
from tests.test_predictions import seed, NOW
from src.analytics.board_cache import refresh_one


async def fixtures(db, count, stats=True):
    target,league,season,teams,history=await seed(db)
    async with db.session() as session:
        if not count:
            match=await session.get(Match,target);match.status='cancelled'
        for index in range(1,count):
            session.add(Match(external_match_id=880000+index,league_id=league,season_id=season,
                home_team_id=teams[index%2],away_team_id=teams[(index+1)%2],round=2,
                status='scheduled',kickoff_at=NOW+timedelta(days=1,hours=index),raw={}))
        if stats:
            for id in history:
                session.add(MatchStatistics(match_id=id,is_final=True,home_corners=2,away_corners=2,
                    home_yellow_cards=1,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,
                    updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit()
    await refresh_one(db,now=NOW,force_league=league)
    return target


async def proxy(route, api):
    path=route.request.url.removeprefix('http://test')
    assert route.request.url.startswith('http://test/')
    assert route.request.method=='GET' or path=='/api/coupon/quote'
    response=await api.request(route.request.method,path,content=route.request.post_data,
        headers={k:v for k,v in route.request.headers.items() if k=='content-type'})
    await route.fulfill(status=response.status_code,body=response.content,
        content_type=response.headers.get('content-type','application/json'))


@pytest.mark.parametrize('width,count',[(w,8) for w in [320,390,768,1024,1536]]+
    [(w,n) for w in [390,1536] for n in [0,1,4]])
async def test_unique_strip_and_single_match_blocks(api,db,monkeypatch,width,count):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await fixtures(db,count)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        await page.clock.install(time=NOW)
        await page.route('http://test/**',lambda route:proxy(route,api))
        try:
            await page.goto('http://test/#predictions?match=14')
            await expect(page.locator('#match-analysis')).to_have_attribute('aria-busy','false')
            cards=page.locator('[data-analysis-match]')
            await expect(cards).to_have_count(count)
            assert await cards.evaluate_all('(els)=>new Set(els.map(e=>e.dataset.analysisMatch)).size')==count
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert '?match=' not in page.url
            if not count:
                await expect(page.locator('#analysis-strip-count')).to_contain_text('0 uygun maç')
                await expect(page.locator('#analysis-selected')).not_to_be_visible()
                await expect(page.locator('#match-analysis-status')).to_contain_text('eşikler düşürülmez')
                return
            await expect(page.locator('[data-analysis-block]')).to_have_count(5)
            await expect(cards.first).to_have_attribute('aria-pressed','true')
            await expect(page.locator('#analysis-advanced')).not_to_have_attribute('open','')
            if count>1:
                await cards.nth(1).evaluate('(e)=>BetAppMatchAnalysis.select(Number(e.dataset.analysisMatch))')
                await expect(cards.nth(1)).to_have_attribute('aria-pressed','true')
                await expect(page.locator('[data-analysis-block]')).to_have_count(5)
                await expect(cards.locator('[aria-pressed=true]')).to_have_count(0)  # No nested duplicate controls.
                await expect(page.locator('[data-analysis-match][aria-pressed=true]')).to_have_count(1)
            for key in ['market','form','source','why','main']:
                await page.locator(f'[data-analysis-block={key}] > summary').click()
                await expect(page.locator(f'[data-analysis-block={key}]')).to_have_attribute('open','')
                await expect(page.locator('#analysis-blocks details[open]')).to_have_count(1)
            await page.locator('[data-analysis-block=main] > summary').click()
            await expect(page.locator('#analysis-blocks details[open]')).to_have_count(0)
            if count==8:
                await page.locator('#analysis-strip-next').click()
                await page.wait_for_function('document.getElementById("analysis-match-strip").scrollLeft>0')
            if width==1536 and count==4:
                await page.locator('#global-search').fill('Test team 0')
                await expect(page.locator('#analysis-advanced')).to_have_attribute('open','')
                await expect(page.locator('#pred-team')).to_have_value('Test team 0')
                await expect(page.locator('#market-picks')).to_contain_text('Test team 0')
            assert not errors
            async with db.session() as session:
                assert await session.scalar(select(func.count()).select_from(SavedCoupon))==0
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


@pytest.mark.parametrize('market',['goals','cards','corners','result','first_half_goals','second_half_goals','asian_handicap','btts'])
async def test_only_relevant_market_and_missing_prices(api,db,monkeypatch,market):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,1)
    detail=(await api.get(f'/api/matches/{target}/statistics')).json()
    candidates=[r for r in detail['candidates'] if r['market']==market and r['qualifies']]
    # A test does not weaken production quality thresholds to create a category.
    if not candidates:
        assert not any(r['market']==market and r['qualifies'] for r in detail['recommendations'])
        return
    page_data=(await api.get('/api/predictions?limit=8')).json()
    page_data['items'][0]['recommendations']=candidates
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        await page.clock.install(time=NOW)
        async def forward(route):
            if route.request.url.endswith('/api/predictions?limit=8'):
                await route.fulfill(json=page_data);return
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions')
            await expect(page.locator('[data-analysis-block=market]')).to_be_visible()
            await page.locator('[data-analysis-block=market] > summary').click()
            body=page.locator('[data-analysis-block=market] .analysis-block-body')
            assert await body.inner_text()
            if market=='corners':
                await expect(body).to_contain_text('Kullanılan / verilen kornerler')
                await expect(body).not_to_contain_text('Atılan / yenilen gol')
            elif market=='cards':
                await expect(body).to_contain_text('Sarı kart')
                await expect(body).not_to_contain_text('Kullanılan / verilen korner')
            elif market in ['goals','result']:
                await expect(body).to_contain_text('Atılan / yenilen gol')
            await page.locator('[data-analysis-block=source] > summary').click()
            for name in ['Crown','Bet365','Sbobet']:
                await expect(page.locator('[data-analysis-block=source]')).to_contain_text(name)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_delayed_detail_and_expired_board_are_never_restored(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,2)
    release=asyncio.Event();entered=asyncio.Event()
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        await page.clock.install(time=NOW)
        async def forward(route):
            if route.request.url.endswith(f'/api/matches/{target}/statistics'):
                entered.set();await release.wait()
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions');await asyncio.wait_for(entered.wait(),5)
            await page.locator('[data-analysis-match]').nth(1).evaluate('(e)=>BetAppMatchAnalysis.select(Number(e.dataset.analysisMatch))')
            await expect(page.locator('[data-analysis-block]')).to_have_count(5)
            title=await page.locator('#analysis-selected-title').inner_text()
            release.set();await page.wait_for_timeout(100)
            await expect(page.locator('#analysis-selected-title')).to_have_text(title)
            await page.clock.fast_forward(601000)
            await expect(page.locator('[data-analysis-match]')).to_have_count(0)
            await expect(page.locator('#analysis-selected')).not_to_be_visible()
            await expect(page.locator('#match-analysis-status')).to_contain_text('Analiz süresi doldu')
        finally:
            release.set();await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_missing_statistics_negative_quote_and_latest_finished_status(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,2)
    first_detail=(await api.get(f'/api/matches/{target}/statistics')).json()
    rec=next(r for r in first_detail['candidates'] if r['market']=='corners' and r['qualifies'])
    page_data=(await api.get('/api/predictions?limit=8')).json()
    page_data['items'][0]['recommendations']=[deepcopy(rec)]
    # Explicit unit fixture only, never used by live capture or application fallback.
    stamp=NOW.isoformat();expiry=(NOW+timedelta(seconds=900)).isoformat()
    rec['verified_odds']={'has_odds':True,'coupon_eligible':True,'freshness':'fresh','expires_at':expiry,
        'observed_at':stamp,'best_price':1.2,'best_bookmaker':'Crown','best_value':-.16,
        'bookmakers':{'Crown':{'current_odds':1.2,'value':-.16,'freshness':'fresh',
            'coupon_eligible':True,'observed_at':stamp,'expires_at':expiry,'line':7.5}}}
    first_detail['recommendations']=[rec];first_detail['candidates']=[rec];first_detail['additional_statistics']={}
    finished=False
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        await page.clock.install(time=NOW)
        async def forward(route):
            url=route.request.url
            if url.endswith('/api/predictions?limit=8'):
                await route.fulfill(json=page_data);return
            if url.endswith(f'/api/matches/{target}/statistics'):
                data=deepcopy(first_detail)
                if finished:data['match']['status']='finished'
                await route.fulfill(json=data);return
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions')
            await expect(page.locator('[data-analysis-block]')).to_have_count(5)
            await page.locator('[data-analysis-block=market] > summary').click()
            await expect(page.locator('[data-analysis-block=market]')).to_contain_text('istatistik sunulmuyor')
            await page.locator('[data-analysis-block=source] > summary').click()
            await expect(page.locator('[data-analysis-block=source] .analysis-negative')).to_have_text('Avantajlı bahis değil')
            await expect(page.locator('[data-analysis-block=source]')).to_contain_text('-16.0%')
            finished=True
            await page.locator(f'[data-analysis-match="{target}"]').evaluate('(e)=>BetAppMatchAnalysis.select(Number(e.dataset.analysisMatch))')
            await expect(page.locator(f'[data-analysis-match="{target}"]')).to_have_count(0)
            await expect(page.locator('[data-analysis-match]')).to_have_count(1)
            await expect(page.locator('[data-analysis-block]')).to_have_count(5)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
