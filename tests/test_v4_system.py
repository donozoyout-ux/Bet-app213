"""User-visible diagnostics and shared monochrome palette; isolated test records only."""
import os
import subprocess
from pathlib import Path
import pytest
from playwright.async_api import async_playwright,expect
from tests.test_minimal_analysis import fixtures,proxy
from tests.test_learning_browser import edition
from tests.test_predictions import NOW


@pytest.mark.parametrize('width',[320,390,768,1024,1536])
async def test_shared_monochrome_routes_and_publication_census(api,db,monkeypatch,width):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await fixtures(db,4);await edition(db)
    data=(await api.get('/api/predictions')).json()
    assert data['cache']['scheduled_matches']==4
    assert data['cache']['statistical_matches']==4
    assert 0<=data['cache']['verified_price_matches']<=4
    assert 0<=data['cache']['quality_matches']<=4
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960},reduced_motion='reduce')
        await page.clock.install(time=NOW)
        baseline=False;errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        capture=Path(os.environ['V4_CAPTURE_DIR']) if os.getenv('V4_CAPTURE_DIR') else None
        old={}
        if capture and width in (390,1536):
            for name in ['dashboard.html','dashboard-market.css','dashboard-market.js','dashboard-analysis.js','dashboard-home.js','dashboard-learning.js','dashboard-coupon.js']:
                old['/' if name=='dashboard.html' else '/'+name]=subprocess.check_output(['git','show','30ae42032e8d6a67d11ca84565dd728161027266:src/api/'+name])
        async def forward(route):
            path=route.request.url.removeprefix('http://test').split('?')[0]
            if baseline and path in old:
                await route.fulfill(body=old[path],content_type='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'text/javascript');return
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            for name in ['home','matches','bulletin','predictions','markets','coupon','performance','lessons']:
                await page.goto('http://test/#'+name)
                await page.wait_for_load_state('networkidle')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),name
                assert await page.evaluate('getComputedStyle(document.body).backgroundColor')=='rgb(255, 255, 255)'
                assert await page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--primary").trim()')=='#111111'
                if name=='home':
                    await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(4)
                    await expect(page.locator('#home-publication-health')).to_contain_text('Planlı maç: 4')
                if name=='predictions':await expect(page.locator('[data-analysis-match]')).to_have_count(4)
                if name=='lessons':await expect(page.locator('.learning-progress').first).to_be_visible()
                if capture and width in (390,1536) and name in ('home','predictions','coupon'):
                    capture.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(capture/f'after-{name}-{width}.png'),full_page=True)
                    baseline=True;await page.reload();await page.wait_for_load_state('networkidle')
                    await page.screenshot(path=str(capture/f'before-{name}-{width}.png'),full_page=True)
                    baseline=False
                    await page.reload();await page.wait_for_load_state('networkidle')
            assert not errors
        finally:await browser.close()
