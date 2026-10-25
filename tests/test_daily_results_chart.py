"""Synthetic persisted records and browser QA only; no production or messaging."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import pytest
from sqlalchemy import select, event
from playwright.async_api import async_playwright, expect
from src.models import Match, PredictionSnapshot, PredictionSettlement
from src.analytics.settlements import settle_snapshot, correct_settlement
from tests.test_performance_status import fixture

NOW=datetime(2026,10,11,12,tzinfo=timezone.utc)


async def history(db):
    ident=await fixture(db)
    async with db.session() as session:
        source=await session.get(Match,ident)
        # 21:30 UTC is the NEXT Istanbul day; two linked picks count once.
        kickoff=datetime(2026,10,9,21,30,tzinfo=timezone.utc)
        for offset,mode in enumerate(['revised','no_ledger','postmatch','pending']):
            match=Match(external_match_id=882200+offset,league_id=source.league_id,season_id=source.season_id,
                home_team_id=source.home_team_id,away_team_id=source.away_team_id,round=1,
                kickoff_at=kickoff,status='scheduled' if mode=='pending' else 'finished',ft_home=2,ft_away=1,raw={})
            session.add(match);await session.flush()
            picks=[dict(id='home',market='result',selection='home',label='Synthetic home win',probability=.8,confidence='high',reliability=.8,sample_size=30)]
            if mode=='revised':picks.append(dict(picks[0],id='linked-home'))
            snapshot=PredictionSnapshot(match_id=match.id,league_id=match.league_id,
                captured_at=kickoff+timedelta(seconds=1) if mode=='postmatch' else kickoff-timedelta(hours=1),
                model_version='synthetic-daily',picks=picks)
            session.add(snapshot);await session.flush()
            if mode=='revised':
                await settle_snapshot(session,snapshot,match,None)
                match.ft_home=0;match.ft_away=1;await session.flush()
                await correct_settlement(session,match.id,0,expected_revision=1,actor='synthetic-test',reason='Synthetic score correction')
        await session.commit()
    return ident


async def test_readonly_unique_persisted_results_revisions_midnight_and_filters(api,db):
    await history(db);statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:response=await api.get('/api/performance-trends?as_of=2026-10-11')
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert response.status_code==200
    data=response.json();day=data['daily'][-1];summary=data['comparisons'][0]['recent']
    assert day['date']=='2026-10-10' and day['won']==0 and day['lost']==day['decided']==1
    assert day['awaiting_data']==1  # finished without persisted ledger is not a win
    assert day['pending']==1
    assert summary['won']==1 and summary['lost']==2 and summary['decided']==3
    assert summary['disputed']==summary['void']==1 and data['duplicate_picks_excluded']==1
    assert len(statements)==2 and all(s.lstrip().startswith('SELECT') for s in statements)
    assert 'LIMIT' in statements[0] and 'kickoff_at >=' in statements[0]
    filtered=(await api.get('/api/performance-trends?as_of=2026-10-11&exact_market=home&confidence=high&market=result')).json()
    assert filtered['comparisons'][0]['recent']['decided']==1
    async with db.session() as session:
        revisions=list(await session.scalars(select(PredictionSettlement).where(PredictionSettlement.revision==2)))
    assert len(revisions)==1  # chart reads never generate or correct settlements


@pytest.mark.parametrize('width',[390,1536])
@pytest.mark.parametrize('state',['sparse','empty','outage','truncated'])
async def test_home_line_chart_real_api_keyboard_touch_and_restart(api,db,monkeypatch,width,state):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    if state!='empty':await history(db)
    if state=='truncated':
        import src.analytics.performance_trends as trends_module
        monkeypatch.setattr(trends_module,'MAX_SNAPSHOTS',0)
    unavailable=state=='outage';calls=[];errors=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960},device_scale_factor=1,
            has_touch=width==390,reduced_motion='reduce')
        await page.clock.install(time=NOW)
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append((route.request.method,path))
            assert route.request.url.startswith('http://test/')
            if path.startswith('/api/performance-trends') and unavailable:
                await route.fulfill(status=503,content_type='application/json',body='{"detail":"Database unavailable"}');return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/')
            panel=page.locator('#home-daily-results')
            await expect(panel).to_have_attribute('aria-busy','false')
            if state=='outage':
                await expect(page.locator('#home-results-settled')).to_have_text('—')
                await expect(page.locator('#home-results-chart')).to_contain_text('Veri bağlantısı bekleniyor')
                await expect(page.locator('#home-results-chart svg')).to_have_count(0)
            elif state=='empty':
                await expect(page.locator('#home-results-settled')).to_have_text('0')
                await expect(page.locator('#home-results-rate')).to_have_text('—')
                await expect(page.locator('#home-results-chart')).to_contain_text('Henüz sonuçlanan tahmin yok')
                await expect(page.locator('#home-results-chart svg')).to_have_count(1)
                await expect(page.locator('#home-results-daily tr')).to_have_count(7)
            elif state=='truncated':
                await expect(page.locator('#home-results-settled')).to_have_text('—')
                await expect(page.locator('#home-results-chart')).to_contain_text('Veri sınırına ulaşıldı')
                await expect(page.locator('#home-results-chart svg')).to_have_count(0)
            else:
                await expect(page.locator('#home-results-settled')).to_have_text('3')
                await expect(page.locator('#home-results-won')).to_have_text('1')
                await expect(page.locator('#home-results-lost')).to_have_text('2')
                await expect(page.locator('#home-results-rate')).to_have_text('%33,3')
                await expect(page.locator('#home-results-chart path[data-series]')).to_have_count(3)
                await expect(page.locator('#home-results-chart circle')).to_have_count(21)
                svg=page.locator('#home-results-chart svg')
                bounds=await svg.bounding_box();assert bounds['height']==(200 if width==390 else 240)
                point=page.locator('#home-results-chart circle[role=button]').first
                await point.focus();await point.press('End')
                await expect(page.locator('#home-results-tooltip')).to_contain_text('10 Ekim 2026')
                await expect(page.locator('#home-results-tooltip')).to_contain_text('Kazandı 0 · Kaybetti 1 · Sonuçlanan 1')
                await expect(page.locator('#home-results-tooltip')).to_contain_text('Başarı %0')
                await point.focus();await point.press('Enter')
                await expect(page.locator('#home-results-tooltip')).to_contain_text('Sonuçlanan tahmin yok')
                assert 'Başarı' not in await page.locator('#home-results-tooltip').inner_text()
                if width==390:await page.locator('#home-results-chart circle').last.tap(force=True)
                else:await page.locator('#home-results-chart circle').last.hover(force=True)
                await expect(page.locator('#home-results-tooltip')).to_contain_text('10 Ekim 2026')
                count=len([path for _,path in calls if path.startswith('/api/performance-trends')])
                await page.locator('#home-results-window').select_option('30')
                await expect(page.locator('#home-results-chart circle')).to_have_count(90)
                await expect(page.locator('#home-results-daily tr')).to_have_count(30)
                await expect(page.locator('#home-results-summary')).to_contain_text('Örneklem yetersiz')
                assert len([path for _,path in calls if path.startswith('/api/performance-trends')])==count
                await page.locator('#home-results-window').select_option('7')
                if width==1536:
                    await page.set_viewport_size({'width':390,'height':960})
                    await expect(svg).to_have_css('height','200px')
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    await page.set_viewport_size({'width':1536,'height':960})
                    await expect(svg).to_have_css('height','240px')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('CHART_SCREENSHOT_DIR'):
                directory=Path(os.environ['CHART_SCREENSHOT_DIR']);directory.mkdir(parents=True,exist_ok=True)
                await page.add_style_tag(content='.terminal-header,.bottom-nav,.skip-link,.floating-tools{visibility:hidden!important}')
                await panel.screenshot(path=str(directory/f'home-results-{state}-{width}.png'))
            if state=='outage':
                unavailable=False;await page.locator('#home-results-retry').click()
                await expect(page.locator('#home-results-settled')).to_have_text('3')
            if state=='sparse':
                unavailable=True;await page.locator('#home-results-retry').click()
                await expect(page.locator('#home-results-settled')).to_have_text('—')
                await expect(page.locator('#home-results-chart svg')).to_have_count(0)
            assert not errors
            assert all(method=='GET' and 'telegram' not in path and 'nvidia' not in path for method,path in calls)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


@pytest.mark.parametrize('width',[390,1536])
@pytest.mark.parametrize('populated',[False,True])
async def test_performance_chart_bounded_height_and_compact_empty(api,db,monkeypatch,width,populated):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    if populated:await history(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960})
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#performance')
            await expect(page.locator('#trend-model')).to_contain_text('2026-10-11 öncesi')
            chart=page.locator('#trend-chart');panel=page.locator('.trend-panel')
            if populated:
                await expect(chart.locator('svg')).to_be_visible()
                height=(await chart.locator('svg').bounding_box())['height']
                assert height==(200 if width==390 else 240)
                await chart.locator('circle').first.focus()
                await expect(page.locator('.trend-tooltip')).to_contain_text('Sonuçlanan')
                if width==1536:
                    await page.set_viewport_size({'width':390,'height':960})
                    await expect(chart.locator('svg')).to_have_css('height','200px')
                    await page.wait_for_function("document.querySelector('#trend-chart svg').viewBox.baseVal.width < 390")
                    await page.set_viewport_size({'width':1536,'height':960})
                    await expect(chart.locator('svg')).to_have_css('height','240px')
                    await page.wait_for_function("document.querySelector('#trend-chart svg').viewBox.baseVal.width > 1000")
            else:
                await expect(chart).to_contain_text('Henüz sonuçlanan tahmin yok')
                await expect(chart.locator('svg')).to_have_count(1)
                assert (await chart.locator('svg').bounding_box())['height']==(200 if width==390 else 240)
                assert (await chart.bounding_box())['height']<420
            assert (await page.locator('.trend-period:visible .trend-bar-track').first.bounding_box())['height']==64
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('CHART_SCREENSHOT_DIR'):
                directory=Path(os.environ['CHART_SCREENSHOT_DIR']);directory.mkdir(parents=True,exist_ok=True)
                await page.add_style_tag(content='.terminal-header,.bottom-nav,.skip-link,.floating-tools{visibility:hidden!important}')
                await panel.screenshot(path=str(directory/f'performance-{"sparse" if populated else "empty"}-{width}.png'))
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
