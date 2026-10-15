"""Coupon disclosure and real ASGI API, with explicitly synthetic stored fixtures."""
import os
from pathlib import Path
import pytest
from playwright.async_api import async_playwright, expect
from tests.test_automatic_telegram import many_matches, NOW


@pytest.mark.parametrize('width,height', [(1920,1080),(2560,1440),(390,844),(320,740)])
async def test_daily_coupon_default_and_tools(api, db, monkeypatch, width, height):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await many_matches(db, 20)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':height},device_scale_factor=1,reduced_motion='reduce')
        await page.clock.install(time=NOW)
        calls=[];errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            assert route.request.url.startswith('http://test/')
            assert not ('telegram' in path and route.request.method=='POST')
            headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key')}
            response=await api.request(route.request.method,path,content=route.request.post_data,headers=headers)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#coupon')
            await expect(page.locator('#coupon-daily')).to_contain_text('kombine oran')
            await page.wait_for_load_state('networkidle')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.evaluate('visualViewport.scale')==1
            label=os.getenv('COUPON_CAPTURE_LABEL','after')
            folder=os.getenv('COUPON_SCREENSHOT_DIR')
            if folder:
                dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(dest/f'{label}-{width}x{height}.png'),full_page=True)
            if label!='before':
                await expect(page.locator('#coupon-tools')).not_to_have_attribute('open','')
                await expect(page.locator('#coupon-selected')).not_to_be_visible()
                await expect(page.locator('#coupon-daily')).to_contain_text('İncelenen toplam maç: 20')
                await expect(page.locator('#coupon-daily .daily-selection')).to_have_count(4)
                await expect(page.locator('#coupon-target')).to_have_count(0)
                await expect(page.locator('#coupon-daily')).to_contain_text('Birleşik risk göstergesi')
                await page.locator('#coupon-decision-details > summary').click()
                await expect(page.locator('#coupon-decision-details .daily-decision')).to_have_count(20)
                await expect(page.locator('#coupon-decision-details')).to_contain_text('Seçildi: risk ve kalite sonrası pozitif katkı')
                await page.locator('#coupon-decision-details > summary').click()
                await page.locator('#coupon-tools > summary').click()
                await expect(page.locator('#coupon-selected')).to_be_visible()
                await expect(page.locator('#coupon-selected button')).to_have_count(4)
                await page.locator('#coupon-manual').click()
                await expect(page.locator('#coupon-edit-panel')).to_have_attribute('open','')
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()
