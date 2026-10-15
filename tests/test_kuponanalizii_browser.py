"""Brand, accessibility and real ASGI interactions at desktop/tablet/mobile sizes."""
from pathlib import Path
import os
import asyncio

import pytest
from playwright.async_api import async_playwright, expect

from tests.test_automatic_telegram import NOW, many_matches


async def test_brand_asset_and_technical_health_contract(api):
    home = await api.get('/')
    assert '<title>Kuponanalizii' in home.text
    assert 'BetApp213' not in home.text
    logo = await api.get('/assets/kuponanalizii-logo.png')
    assert logo.status_code == 200
    assert logo.headers['content-type'] == 'image/png'
    assert logo.content.startswith(b'\x89PNG\r\n\x1a\n')
    assert 'max-age=86400' in logo.headers['cache-control']
    # Public health remains the technical identity; API consumers are unaffected.
    assert (await api.get('/health')).json() == {'status': 'ok', 'service': 'BetApp213'}
    assert (await api.get('/assets/unknown.png')).status_code == 404


@pytest.mark.parametrize('width', [1536, 1024, 768, 390, 320])
async def test_brand_navigation_coupon_and_accessible_motion(api, db, monkeypatch, width):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await many_matches(db, 4)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
        await page.clock.install(time=NOW)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
        calls = []

        async def forward(route):
            path = route.request.url.removeprefix('http://test')
            calls.append(path)
            assert 'api.telegram.org' not in route.request.url
            headers = {k: v for k, v in route.request.headers.items()
                       if k in ('content-type', 'x-coupon-key', 'authorization')}
            response = await api.request(route.request.method, path,
                                         content=route.request.post_data, headers=headers)
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get('content-type', 'application/json'))

        await page.route('**/*', forward)
        screenshots = os.getenv('UI_SCREENSHOT_DIR')

        async def capture(name):
            if screenshots:
                target = Path(screenshots)
                target.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(target / f'{name}-{width}.png'), full_page=True)

        try:
            await page.goto('http://test/')
            await page.wait_for_load_state('networkidle')
            await expect(page).to_have_title('Kuponanalizii · Futbol Analiz Merkezi')
            logo = page.locator('.brand-symbol img')
            await expect(logo).to_be_visible()
            assert await logo.evaluate('(el)=>el.complete && el.naturalWidth>0')
            assert await page.locator('body').evaluate('(el)=>getComputedStyle(el).color') == 'rgb(244, 247, 255)'
            assert await page.locator('.prediction-card').first.evaluate('(el)=>getComputedStyle(el).transitionDuration') == '0s'
            assert await page.locator('.brand').get_attribute('aria-label') == 'Kuponanalizii Ana Sayfa'
            assert await page.locator('link[rel=icon]').get_attribute('href') == '/assets/kuponanalizii-logo.png'
            if width > 1150:
                assert await page.locator('.home-summary').evaluate(
                    '(el)=>getComputedStyle(el).gridTemplateColumns.split(" ").length') == 5
            await expect(page.locator('a:has-text("Premium"),a:has-text("Hesabım"),a:has-text("AI Tahmin")')).to_have_count(0)
            await capture('home')
            # Hero action reaches the existing automatic coupon API, not a demo state.
            # Navigation is synchronous, but coupon creation is a server request
            # with a 10s budget. Wait for that response before checking its DOM.
            async with page.expect_response(lambda r: '/api/coupons/automatic?strategy=balanced' in r.url, timeout=15000) as created:
                await page.locator('.hero-actions .primary-button').click()
            response=await created.value
            assert response.status==200
            assert len((await response.json())['items'])==4
            await expect(page.locator('[data-page="coupon"]')).to_be_visible()
            await expect(page.locator('#coupon-selected button')).to_have_count(4)
            await expect(page.locator('#coupon-day')).to_have_text(NOW.date().isoformat())
            await expect(page.locator('#coupon-total')).to_contain_text('23,43')
            await expect(page.locator('#coupon-target')).to_have_count(0)
            await expect(page.locator('#coupon-strategy')).to_have_value('balanced')
            assert any('/api/coupons/automatic?strategy=balanced' in path for path in calls)
            await capture('coupon')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # Editing stays accessible outside the default daily view.
            await expect(page.locator('#coupon-selected')).not_to_be_visible()
            await page.locator('#coupon-tools > summary').click()
            await page.locator('#coupon-selected button').first.click()
            await expect(page.locator('#coupon-mode')).to_contain_text('Manuel')
            await expect(page.locator('#coupon-selected button')).to_have_count(3)
            await expect(page.locator('#coupon-telegram')).to_be_disabled()
            nav = '.bottom-nav' if width <= 600 else '.desktop-nav'
            for section, title in [('matches', 'Canlı Skor'), ('predictions', 'Tahminler'),
                                   ('markets', 'İstatistikler'), ('performance', 'Geçmiş Performans'),
                                   ('coupon', 'Kupon Analizi'), ('home', 'Futbol Analiz Merkezi')]:
                await page.locator(f'{nav} [data-page-link="{section}"]').click()
                await expect(page.locator(f'[data-page="{section}"]')).to_be_visible()
                await expect(page).to_have_title('Kuponanalizii · ' +
                    ('Canlı Skor & Maç Merkezi' if section == 'matches' else title))
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if width == 1536:
                await page.locator(f'{nav} [data-page-link="coupon"]').click()
                await page.locator('#coupon-manual').click()
                await page.locator('#coupon-strategy').select_option('cautious')
                await page.locator('#coupon-automatic').click()
                await expect(page.locator('#coupon-daily')).to_contain_text('Dinamik seçim')
                assert any('strategy=cautious' in path and 'max_matches' not in path for path in calls)
                await page.locator('#coupon-manual').click()
                await expect(page.locator('#coupon-selected button')).to_have_count(3)
            if width <= 600:
                await page.locator('#search-toggle').click()
                await expect(page.locator('#global-search')).to_be_focused()
            assert not any('/telegram/verify' in path or path.endswith('/telegram') for path in calls)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def test_tablet_coupon_waits_for_slow_automatic_response(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await many_matches(db,4)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':768,'height':960},reduced_motion='reduce')
        await page.clock.install(time=NOW)
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            assert 'api.telegram.org' not in route.request.url
            if path.startswith('/api/coupons/automatic') and '#coupon' in page.url:
                # A valid response can exceed Playwright's default 5s assertion
                # window while still meeting the endpoint's 10s server budget.
                await asyncio.sleep(5.25)
            headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key')}
            response=await api.request(route.request.method,path,content=route.request.post_data,headers=headers)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/')
            await page.wait_for_load_state('networkidle')
            async with page.expect_response(lambda r:'/api/coupons/automatic?strategy=balanced' in r.url,timeout=15000) as created:
                await page.locator('.hero-actions .primary-button').click()
            response=await created.value
            assert response.status==200 and len((await response.json())['items'])==4
            await expect(page.locator('#coupon-selected button')).to_have_count(4)
            await expect(page.locator('#coupon-total')).to_contain_text('23,43')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('#coupon-tools > summary').click()
            await page.locator('#coupon-selected button').first.click()
            await expect(page.locator('#coupon-selected button')).to_have_count(3)
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


@pytest.mark.parametrize('motion', ['reduce', 'no-preference'])
async def test_live_scores_bookmaker_controls_and_pulse(api, db, monkeypatch, motion):
    from sqlalchemy import select
    from src.models import Match
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await many_matches(db, 4)
    async with db.session() as session:
        match = await session.scalar(select(Match).where(Match.status == 'scheduled'))
        match.status = 'live'
        match.ft_home, match.ft_away = 2, 1
        match.live_minute = 67
        await session.commit()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion=motion)

        async def forward(route):
            response = await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get('content-type', 'application/json'))

        await page.route('http://test/**', forward)
        try:
            await page.goto('http://test/#matches')
            await page.locator('[data-view=live]').click()
            row = page.locator('#matches-body .is-live')
            await expect(row).to_have_count(1)
            await expect(row).to_contain_text('67′')
            await expect(row).to_contain_text('2 - 1')
            assert await row.locator('td').first.evaluate(
                '(el)=>getComputedStyle(el,"::before").animationName') == (
                    'none' if motion == 'reduce' else 'live-pulse')
            for bookmaker in ['Bet365', 'Sbobet', 'Crown']:
                button = page.locator(f'.match-bookmakers [data-bookmaker={bookmaker}]')
                await button.click()
                await expect(button).to_have_attribute('aria-pressed', 'true')
                await expect(page.locator('#matches-count')).to_contain_text(bookmaker)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()
