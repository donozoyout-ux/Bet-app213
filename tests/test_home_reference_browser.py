"""Real ASGI/SQLite fixtures, at 100% zoom. No production or Telegram writes."""
import json
import os
import subprocess
from pathlib import Path
from datetime import timedelta

import pytest
from playwright.async_api import async_playwright, expect
from sqlalchemy import select
from src.models import Match, Odds1X2, Bookmaker
from tests.odds_fixture import observed_prices
from tests.test_automatic_telegram import many_matches, NOW

SIZES = [(1440,900),(1920,1080),(2560,1440),(3840,2160),
         (1024,900),(768,1024),(390,844),(320,740)]


@pytest.mark.parametrize('width,height', SIZES)
async def test_reference_home_layout_and_real_interactions(api, db, monkeypatch, width, height):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    await many_matches(db, 4)
    async with db.session() as session:
        first = await session.scalar(select(Match).where(Match.status=='scheduled'))
        live = Match(external_match_id=771122, league_id=first.league_id, season_id=first.season_id,
                     home_team_id=first.home_team_id, away_team_id=first.away_team_id,
                     round=3,status='live',live_minute='67',ft_home=2,ft_away=1,
                     kickoff_at=NOW-timedelta(hours=1),status_observed_at=NOW,created_at=NOW,updated_at=NOW,raw={})
        session.add(live)
        book = await session.scalar(select(Bookmaker).where(Bookmaker.name=='Bet365'))
        row=await session.scalar(select(Odds1X2).where(Odds1X2.match_id==first.id,Odds1X2.bookmaker_id==book.id))
        if row is None:
            row=Odds1X2(match_id=first.id,bookmaker_id=book.id);session.add(row)
        row.opening_home=1.8;row.latest_home=1.9;row.latest_draw=3.2;row.latest_away=4.1
        row.updated_at=NOW;row.raw=observed_prices(NOW,'latest_home','latest_draw','latest_away')
        await session.commit()
    await api.post('/api/coupons/automatic')
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width':width,'height':height},
                                      device_scale_factor=1,reduced_motion='reduce')
        await page.clock.install(time=NOW)
        errors=[]; calls=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)

        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            assert route.request.url.startswith('http://test/')
            assert '/telegram' not in path
            if os.getenv('HOME_CAPTURE_LABEL')=='before' and path in ('/','/dashboard-market.css','/dashboard-market.js'):
                filename={'/':'dashboard.html','/dashboard-market.css':'dashboard-market.css','/dashboard-market.js':'dashboard-market.js'}[path]
                content=subprocess.check_output(['git','show','origin/master:src/api/'+filename])
                await route.fulfill(body=content,content_type='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'application/javascript')
                return
            headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key')}
            response=await api.request(route.request.method,path,content=route.request.post_data,headers=headers)
            await route.fulfill(status=response.status_code,body=response.content,
                                content_type=response.headers.get('content-type','application/json'))

        await page.route('**/*',forward)
        try:
            await page.goto('http://test/')
            await expect(page.locator('#prediction-cards .prediction-card')).to_have_count(4)
            await page.wait_for_load_state('networkidle')
            label=os.getenv('HOME_CAPTURE_LABEL','after')
            if label!='before':
                await expect(page.locator('#home-coupon-list .coupon-fixture')).to_have_count(4)
                await expect(page.locator('#home-coupon-total')).to_contain_text('23,43')
                await expect(page.locator('#home-live-list')).to_contain_text('2 – 1')
                await expect(page.locator('#home-movement-list')).to_contain_text('1,80')
                await expect(page.locator('#home-movement-list')).to_contain_text('1,90')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.evaluate('visualViewport.scale')==1
            assert await page.locator('body').evaluate('(el)=>getComputedStyle(el).zoom')=='1'
            metrics=await page.evaluate('''() => {
                const rect=s=>{const r=document.querySelector(s)?.getBoundingClientRect();return r?{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom}:null};
                return {hero:rect('.platform-intro'),board:rect('.home-board'),coupon:rect('.home-coupon'),
                        titleSize:parseFloat(getComputedStyle(document.querySelector('h1')).fontSize),
                        bodySize:parseFloat(getComputedStyle(document.body).fontSize),fonts:{html:getComputedStyle(document.documentElement).fontFamily,body:getComputedStyle(document.body).fontFamily,p:getComputedStyle(document.querySelector('p')).fontFamily},heights:{list:rect('.coupon-list'),footer:rect('.coupon-panel-footer'),heading:rect('.home-coupon .panel-heading')}};
            }''')
            folder=os.getenv('HOME_SCREENSHOT_DIR')
            if folder:
                dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(dest/f'{label}-{width}x{height}.png'))
                (dest/f'{label}-{width}x{height}.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
            if label!='before' and width>=1440:
                assert metrics['board']['width']>=(width-metrics['board']['x'])*.90
                assert metrics['coupon']['width']/metrics['board']['width']>=.40
                assert metrics['hero']['height']<=height*.45
                # Readable four-leg content may scroll; the panel must start above the fold.
                assert metrics['coupon']['y']<height*.8
                assert metrics['coupon']['height']<=height*.8
                assert 28<=metrics['titleSize']<=48  # compact V4 hero, including wide screens
                assert 16<=metrics['bodySize']<=20
            if label!='before':
                await page.locator('#home-coupon-open').click()
                await expect(page.locator('[data-page=coupon]')).to_be_visible()
                await expect(page.locator('#coupon-selected button')).to_have_count(4)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


def test_home_javascript_runtime():
    import shutil
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable; supplied in CI')
    subprocess.run([node,'--check','src/api/dashboard-home.js'],check=True,capture_output=True,text=True)
    subprocess.run([node,'tests/home_runtime.cjs'],check=True,capture_output=True,text=True)
