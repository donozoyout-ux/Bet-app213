"""Chromium exercises rendered UI against real FastAPI responses from a test DB."""
from datetime import timedelta
import pytest
from src.models import MatchStatistics
from tests.test_predictions import seed, NOW


async def test_browser_count_markets_coverage_dialog_and_failed_request(api,db,monkeypatch):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,_,_,_,history=await seed(db)
    async with db.session() as session:
        for id in history:
            session.add(MatchStatistics(match_id=id,is_final=True,home_corners=2,away_corners=2,
                home_yellow_cards=1,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,
                updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit()
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':1280,'height':900})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        fail=False
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if fail and path.startswith('/api/market-performance'):
                await route.fulfill(status=503,body='Unavailable')
                return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        await page.goto('http://test/')
        await page.locator('#upcoming-board').click()
        for metric in ('corners','cards'):
            await page.locator(f'[data-market-family="{metric}"]').click()
            await expect(page.locator('#market-picks tr[data-match-id]')).to_have_count(1)
            await expect(page.locator('#market-picks')).to_contain_text('Test team')
        await page.locator('#market-picks tr[data-match-id]').click()
        await expect(page.locator('#analysis-dialog')).to_be_visible()
        await page.keyboard.press('Escape')
        await expect(page.locator('#analysis-dialog')).not_to_be_visible()
        await expect(page.locator('#stats-league-coverage')).to_contain_text('80')
        # An independent performance request failure must not discard the board.
        fail=True
        await page.locator('#performance-market').select_option('cards')
        await expect(page.locator('#performance-summary')).to_contain_text('Bağlantı bekleniyor')
        await expect(page.locator('#market-picks tr[data-match-id]')).to_have_count(1)
        # An honest empty state explains the filter rather than inventing a pick.
        await page.locator('#min-probability').fill('100')
        await page.locator('#min-probability').dispatch_event('change')
        await expect(page.locator('#market-picks')).to_contain_text('filtresi dışında')
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert errors==[]
        await browser.close()
