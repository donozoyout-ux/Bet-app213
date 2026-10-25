"""Navigation acceptance in Chromium; every request stays in isolated ASGI fixtures."""
import json
import os
import subprocess
from datetime import timedelta
from sqlalchemy import select
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from tests.test_automatic_telegram import NOW, many_matches
from tests.ui_palette import assert_monochrome

PAGES = ['home', 'matches', 'coupon', 'predictions', 'markets', 'performance', 'lessons']


@pytest.mark.parametrize('width', [390, 1536])
@pytest.mark.parametrize('motion', ['reduce', 'no-preference'])
async def test_top_navigation_and_motion(api, db, monkeypatch, width, motion):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await many_matches(db, 4)
    from src.models import Match
    from src.analytics.settlements import settle_match
    from tests.test_learning_browser import edition
    await edition(db, title='Gözlenen sonuçlar · izole test örneklemi')
    async with db.session() as session:
        first = await session.scalar(select(Match).where(Match.status=='scheduled'))
        first.status='finished'; first.ft_home=2; first.ft_away=1
        await settle_match(session, first)
        session.add(Match(external_match_id=772211, league_id=first.league_id, season_id=first.season_id,
            home_team_id=first.home_team_id, away_team_id=first.away_team_id, round=3,
            kickoff_at=NOW-timedelta(hours=1), status='live', live_minute='67', ft_home=2, ft_away=1,
            status_observed_at=NOW, created_at=NOW, updated_at=NOW, raw={}))
        await session.commit()
    await api.post('/api/coupons/automatic')
    before = os.getenv('NAV_CAPTURE_PHASE') == 'before'
    if before and motion != 'reduce':
        pytest.skip('Baseline captures need one motion setting only')
    calls, errors, metrics = [], [], []
    baseline_assets = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': width, 'height': 960}, reduced_motion=motion)
        await page.clock.install(time=NOW)
        page.on('pageerror', lambda error: errors.append(str(error)))

        async def forward(route):
            assert route.request.url.startswith('http://test/')
            path = route.request.url.removeprefix('http://test')
            calls.append((route.request.method, path))
            if before and (path=='/' or path.endswith(('.css','.js'))):
                filename='dashboard.html' if path=='/' else path.lstrip('/')
                if path not in baseline_assets:
                    baseline_assets[path]=subprocess.check_output(['git','show','12e1f10:src/api/'+filename])
                await route.fulfill(body=baseline_assets[path], content_type='text/html' if path=='/' else
                                    'text/css' if path.endswith('.css') else 'application/javascript')
                return
            assert not any(x in path for x in ['/telegram/verify', '/nvidia'])
            response = await api.request(route.request.method, path, content=route.request.post_data,
                                         headers={k:v for k,v in route.request.headers.items()
                                                  if k in ('content-type', 'x-coupon-key')})
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get('content-type', 'application/json'))

        await page.route('**/*', forward)
        try:
            await page.goto('http://test/')
            await page.wait_for_load_state('networkidle')
            nav = page.locator('.bottom-nav' if before and width == 390 else '#primary-navigation')
            await page.evaluate('window.navigationSentinel = 134')
            for name in PAGES:
                await nav.locator(f'[data-page-link={name}]').click()
                section = page.locator(f'[data-page={name}]')
                await expect(section).to_be_visible()
                await section.evaluate("el=>el.querySelectorAll('details').forEach(d=>d.open=true)")
                await page.wait_for_load_state('networkidle')
                await expect(page.locator('[data-page]:visible')).to_have_count(1)
                await expect(nav.locator('[aria-current=page]')).to_have_count(1)
                await expect(nav.locator(f'[data-page-link={name}]')).to_have_attribute('aria-current', 'page')
                assert await page.evaluate('window.navigationSentinel') == 134
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (name, await page.evaluate("[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>innerWidth+1 && el.getClientRects().length).slice(0,20).map(el=>[el.tagName,el.className,Math.round(el.getBoundingClientRect().right)])"))
                assert page.url.endswith('#' + name)
                if not before:
                    assert await section.evaluate('(el)=>getComputedStyle(el).animationDuration') == ('0s' if motion == 'reduce' else '0.3s')
                    assert await page.locator('body').evaluate('(el)=>getComputedStyle(el).paddingLeft') == '0px'
                    assert await page.locator('body').evaluate('(el)=>getComputedStyle(el).backgroundColor') == 'rgb(255, 255, 255)'
                    await assert_monochrome(section)
                if os.getenv('NAV_SCREENSHOT_DIR') and motion == 'reduce':
                    folder = Path(os.environ['NAV_SCREENSHOT_DIR']); folder.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(folder/f'{"before" if before else "after"}-{name}-{width}.png'))
                metrics.append({'page': name, 'width': width, 'motion': motion, 'overflow': False})
            if not before:
                await expect(page.locator('.app-sidebar, .bottom-nav, #sidebar-toggle')).to_have_count(0)
                assert await nav.evaluate('(el)=>el.scrollWidth>el.clientWidth') == (width == 390)
                links = nav.locator('a')
                await links.first.focus()
                await page.keyboard.press('ArrowRight')
                await expect(links.nth(1)).to_be_focused()
                await page.keyboard.press('Enter')
                await expect(page.locator('[data-page=matches]')).to_be_visible()
                await links.first.focus()
                await page.keyboard.press('End')
                await expect(links.last).to_be_focused()
                await page.keyboard.press('Home')
                await expect(links.first).to_be_focused()
                await page.keyboard.press('Tab')
                await expect(links.nth(1)).to_be_focused()
                assert await links.nth(1).evaluate('(el)=>getComputedStyle(el).outlineStyle') != 'none'
                await links.first.click()
                await page.go_back()
                await expect(page.locator('[data-page=matches]')).to_be_visible()
                await page.go_forward()
                await expect(page.locator('[data-page=home]')).to_be_visible()
                if width == 390:
                    await page.locator('#search-toggle').click()
                    await expect(page.locator('#global-search')).to_be_focused()
                    await page.keyboard.press('Escape')
                    await expect(page.locator('#search-toggle')).to_be_focused()
                    await expect(page.locator('#search-toggle')).to_have_attribute('aria-expanded', 'false')
                # Inspect a new content animation before its 300 ms duration elapses.
                await page.evaluate('BetAppUI.navigate("coupon")')
                animation = await page.locator('[data-page=coupon]').evaluate('''el=>({
                    count:el.getAnimations().length, duration:getComputedStyle(el).animationDuration})''')
                assert animation['count'] == (0 if motion == 'reduce' else 1)
                if motion != 'reduce':
                    halfway = await page.locator('[data-page=coupon]').evaluate('''el=>{
                        const a=el.getAnimations()[0];a.pause();a.currentTime=150;
                        return {opacity:Number(getComputedStyle(el).opacity),transform:getComputedStyle(el).transform};}''')
                    assert 0 < halfway['opacity'] < 1 and halfway['transform'] != 'none'
                    if os.getenv('NAV_SCREENSHOT_DIR'):
                        await page.screenshot(path=str(Path(os.environ['NAV_SCREENSHOT_DIR'])/f'transition-{width}.png'))
                    await page.locator('[data-page=coupon]').evaluate('el=>el.getAnimations().forEach(a=>a.finish())')
                await page.wait_for_timeout(350)
                await links.nth(1).hover()
                await page.wait_for_timeout(350)
                hover = await links.nth(1).evaluate('''el=>({transform:getComputedStyle(el).transform,
                    shadow:getComputedStyle(el).boxShadow,underline:getComputedStyle(el,'::after').transform})''')
                if motion == 'reduce':
                    assert hover['transform'] == 'none'
                else:
                    assert hover['transform'] != 'none' and hover['shadow'] != 'none'
                    assert hover['underline'] == 'matrix(1, 0, 0, 1, 0, 0)'
                    if os.getenv('NAV_SCREENSHOT_DIR'):
                        await page.locator('.terminal-header').screenshot(path=str(Path(os.environ['NAV_SCREENSHOT_DIR'])/f'hover-{width}.png'))
            assert not errors
            assert not any(method != 'GET' and '/telegram' in path for method,path in calls)
            if os.getenv('NAV_SCREENSHOT_DIR'):
                folder=Path(os.environ['NAV_SCREENSHOT_DIR']);folder.mkdir(parents=True,exist_ok=True)
                (folder/f'{"before" if before else "after"}-{width}-{motion}.json').write_text(
                    json.dumps(metrics, indent=2), encoding='utf-8')
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()
