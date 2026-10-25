"""Chromium exercises rendered UI against real FastAPI responses from a test DB."""
from datetime import timedelta
import os
from pathlib import Path
from tests.ui_palette import assert_monochrome
import pytest
from src.models import MatchStatistics
from tests.test_predictions import seed, NOW


@pytest.mark.parametrize('width', [390,1536])
async def test_browser_count_markets_coverage_dialog_and_failed_request(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,league,_,_,history=await seed(db)
    async with db.session() as session:
        for id in history:
            session.add(MatchStatistics(match_id=id,is_final=True,home_corners=2,away_corners=2,
                home_yellow_cards=1,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,
                updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit()
    from src.analytics.board_cache import refresh_one
    await refresh_one(db, now=NOW, force_league=league)

    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960})
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
        await page.locator('.desktop-nav [data-page-link=predictions]').click()
        await page.locator('#analysis-advanced > summary').click()
        await page.locator('#upcoming-board').click()
        for metric in ('corners','cards'):
            await page.locator('#pred-market').select_option(metric)
            expected=(await api.get(f'/api/predictions/best?statistical_all=true&market={metric}&min_probability=0.6')).json()['total']
            assert expected>1  # All statistical lines, not just the selected recommendation.
            await expect(page.locator('#market-picks tr[data-match-id]')).to_have_count(expected)
            await expect(page.locator('#market-picks')).to_contain_text('Test team')
        await page.locator('#market-picks tr[data-match-id]').first.click()
        await expect(page.locator('#analysis-dialog')).to_be_visible()
        dialog=page.locator('#analysis-dialog')
        for tab in await dialog.locator('[data-analysis-tab]').all():
            await tab.click()
            await dialog.evaluate("el=>el.querySelectorAll('details').forEach(d=>d.open=true)")
            await page.wait_for_load_state('networkidle')
            await assert_monochrome(dialog)
        await dialog.locator('[data-analysis-tab=summary]').click()
        await page.wait_for_load_state('networkidle')
        if os.getenv('NAV_SCREENSHOT_DIR'):
            await page.screenshot(path=str(Path(os.environ['NAV_SCREENSHOT_DIR'])/f'after-dialog-{width}.png'))
        await page.keyboard.press('Escape')
        await expect(page.locator('#analysis-dialog')).not_to_be_visible()
        await page.locator('.desktop-nav [data-page-link=markets]').click()
        await expect(page.locator('#stats-league-coverage')).to_contain_text('80')
        await page.locator('.desktop-nav [data-page-link=performance]').click()
        # An independent performance request failure must not discard the board.
        fail=True
        await page.locator('#performance-market').select_option('cards')
        await expect(page.locator('#performance-summary')).to_contain_text('PostgreSQL')
        await page.locator('.desktop-nav [data-page-link=predictions]').click()
        await expect(page.locator('#market-picks tr[data-match-id]')).to_have_count(expected)
        # An honest empty state explains the filter rather than inventing a pick.
        await page.locator('#min-probability').fill('100')
        await page.locator('#min-probability').dispatch_event('change')
        await expect(page.locator('#market-picks')).to_contain_text('filtresi dışında')
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        assert errors==[]
        await browser.close()
