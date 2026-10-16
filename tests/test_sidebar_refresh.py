"""Regression coverage for independent model / quote views and browser expiry."""
from datetime import timedelta
import asyncio
import os
from pathlib import Path
import subprocess

import pytest
from playwright.async_api import async_playwright, expect
from src.models import Match
from tests.test_predictions import seed, NOW


async def test_statistical_view_paginates_before_price_filter_and_rechecks_status(api, db, monkeypatch):
    import src.api.routes as routes
    from src.analytics.board_cache import refresh_one
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    target, league, *_ = await seed(db)
    await refresh_one(db, now=NOW, force_league=league)
    data=(await api.get('/api/predictions/best?statistical_all=true&limit=100')).json()
    assert data['total'] > 2
    assert data['cache']['checked_at']
    first=(await api.get('/api/predictions/best?statistical_all=true&limit=1')).json()
    second=(await api.get('/api/predictions/best?statistical_all=true&limit=1&offset=1')).json()
    assert first['items'][0]['recommendation']['id'] != second['items'][0]['recommendation']['id']
    priced=(await api.get('/api/predictions/best?statistical_all=true&priced_only=true')).json()
    assert priced['total']>0
    assert all(i['recommendation']['verified_odds']['coupon_eligible'] for i in priced['items'])
    async with db.session() as session:
        match=await session.get(Match,target)
        match.status='finished'
        await session.commit()
    for path in ['/api/predictions', '/api/predictions/best?statistical_all=true']:
        assert not (await api.get(path)).json()['items']


async def test_browser_expires_fixture_during_delayed_refresh_and_marks_stale(api):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':1024,'height':900})
        stamp=NOW.isoformat()
        fixture={'id':999,'status':'scheduled','kickoff_at':(NOW+timedelta(seconds=5)).isoformat(),
                 'home_team':'Expiry Home','away_team':'Expiry Away','league':'Test league'}
        rec={'label':'Ev Sahibi','market':'result','probability':.7,'sample_size':50,'confidence':'high',
             'verified_odds':{'has_odds':False,'reason':'line_mismatch'}}
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/predictions?'):
                await route.fulfill(json={'items':[{'match':fixture,'recommendations':[rec]}],
                    'generated_at':stamp,'cache':{'checked_at':stamp,'stale':True}})
                return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.clock.install(time=NOW)
            await page.goto('http://test/')
            await expect(page.locator('#prediction-cards')).to_contain_text('Expiry Home')
            await expect(page.locator('#home-prediction-note')).to_contain_text('güncel değil')
            await page.clock.fast_forward(6000)
            await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(0)
            await page.locator('#sidebar-toggle').click()
            await expect(page.locator('body')).to_have_class('sidebar-collapsed')
            await expect(page.get_by_role('navigation',name='Ana gezinme',exact=True).get_by_role('link',name='Tahminler',exact=True)).to_be_visible()
            await page.reload()
            await expect(page.locator('#sidebar-toggle')).to_have_attribute('aria-expanded','false')
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def test_delayed_snapshot_cannot_restore_started_match_and_midnight_refreshes(api):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page()
        release=asyncio.Event()
        entered=asyncio.Event()
        calls=[]
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/predictions?'):
                calls.append(path)
                entered.set()
                await release.wait()
                await route.fulfill(json={'items':[{'match':{'id':999,'status':'scheduled',
                    'kickoff_at':(NOW+timedelta(seconds=5)).isoformat(),'home_team':'Delayed',
                    'away_team':'Expired','league':'Test'},'recommendations':[{'label':'Ev',
                    'probability':.7,'sample_size':50,'confidence':'high'}]}],
                    'generated_at':NOW.isoformat(),'cache':{'checked_at':NOW.isoformat()}})
                return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.clock.install(time=NOW)
            await page.goto('http://test/')
            await asyncio.wait_for(entered.wait(),5)
            await page.clock.fast_forward(7000)
            release.set()
            await expect(page.locator('#prediction-cards')).to_have_attribute('aria-busy','false')
            await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(0)
            before=len(calls)
            await page.clock.fast_forward(24*3600*1000)
            await page.wait_for_function('document.getElementById("prediction-cards").getAttribute("aria-busy")==="false"')
            assert len(calls)>before
            await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(0)
        finally:
            release.set()
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def test_negative_value_quote_evidence_expiry_and_view_query(api):
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page()
        queries=[]
        match={'id':999,'status':'scheduled','kickoff_at':(NOW+timedelta(days=1)).isoformat(),
               'home_team':'Quote Home','away_team':'Quote Away','league':'Test'}
        quote={'has_odds':True,'coupon_eligible':True,'freshness':'fresh','best_price':1.2,
               'best_value':-.16,'best_bookmaker':'Crown','best_implied_probability':1/1.2,
               'expires_at':(NOW+timedelta(seconds=900)).isoformat(),
               'bookmakers':{'Crown':{'current_odds':1.2,'freshness':'fresh','observed_at':NOW.isoformat()}}}
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/predictions/best'):
                queries.append(path)
                rec={'label':'Ev Sahibi','market':'result','probability':.7,'sample_size':50,
                     'confidence':'high','reliability':.85,'verified_odds':quote}
                await route.fulfill(json={'items':[{'match':match,'recommendation':rec}],
                    'evaluated_matches':1,'total':1,'generated_at':NOW.isoformat(),
                    'cache':{'checked_at':NOW.isoformat()}})
                return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.clock.install(time=NOW)
            await page.goto('http://test/#predictions')
            await expect(page.locator('#market-picks .negative-value')).to_contain_text('-16.0%')
            await expect(page.locator('#market-picks')).to_contain_text('Avantajlı bahis değil')
            await expect(page.locator('#market-picks')).to_contain_text('Kanıt:')
            await page.locator('#pred-view').select_option('verified')
            await page.wait_for_function('document.getElementById("market-picks").getAttribute("aria-busy")==="false"')
            assert 'priced_only=true' in queries[-1] and 'offset=0' in queries[-1]
            await page.clock.fast_forward(901000)
            await expect(page.locator('#market-picks')).to_contain_text('güncelliği yitirilmiş')
            await expect(page.locator('#market-picks .negative-value')).to_have_count(0)
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


@pytest.mark.skipif(not os.getenv("CAPTURE_REVIEW_SCREENSHOTS"),reason="Explicit review artifact capture")
async def test_capture_review_screenshots(api, db, monkeypatch):
    import src.api.routes as routes
    from src.analytics.board_cache import refresh_one
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    _,league,*_=await seed(db)
    await refresh_one(db,now=NOW,force_league=league)
    output=Path('docs/sidebar-refresh')
    output.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        for baseline,width in [(True,1536),(False,320),(False,390),(False,768),(False,1024),(False,1536)]:
            page=await browser.new_page(viewport={'width':width,'height':960},reduced_motion='reduce')
            async def forward(route):
                path=route.request.url.removeprefix('http://test')
                # Resolve the actual asset basename without external resources.
                if baseline and (path=='/' or path.endswith('/dashboard-market.css') or path.endswith('/dashboard-market.js')):
                    name='dashboard.html' if path=='/' else path.rsplit('/',1)[-1]
                    body=subprocess.check_output(['git','show','origin/master:src/api/'+name])
                    await route.fulfill(body=body,content_type='text/html' if path=='/' else 'text/css' if name.endswith('.css') else 'application/javascript')
                    return
                response=await api.get(path)
                await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
            await page.route('http://test/**',forward)
            await page.goto('http://test/#predictions')
            await expect(page.locator('#market-picks tr[data-match-id]')).not_to_have_count(0)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('h1:visible').evaluate('(el)=>el.blur()')
            await page.screenshot(path=str(output/f'{"before" if baseline else "after"}-{width}.png'),full_page=True)
            await page.unroute_all(behavior='ignoreErrors')
            await page.close()
        await browser.close()
