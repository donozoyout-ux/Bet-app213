"""Real read-only API and Chromium acceptance for completed versus current days."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import os
import subprocess
import pytest
from sqlalchemy import select,event
from playwright.async_api import async_playwright,expect
from src.models import League,Season,Team,Match,PredictionSnapshot
from src.analytics.settlements import settle_snapshot

NOW=datetime(2026,10,25,12,tzinfo=timezone.utc)


async def cohort(db,mode):
    async with db.session() as s:
        league=await s.scalar(select(League).where(League.external_id==36))
        season=Season(league_id=league.id,season_name='2026-2027');s.add(season)
        teams=[Team(external_id=880001+i,name='Grafik test '+str(i)) for i in range(2)]
        s.add_all(teams);await s.flush()
        cases=[] if mode=='empty' else ['pending'] if mode=='pending' else ['awaiting'] if mode=='awaiting' else ['won','lost','pending','awaiting']
        for i,case in enumerate(cases):
            # 21:30 UTC belongs to the following Istanbul day.
            kickoff=NOW.replace(hour=21,minute=30)-timedelta(days=1)
            m=Match(external_match_id=881000+i,league_id=league.id,season_id=season.id,round=1,
                home_team_id=teams[0].id,away_team_id=teams[1].id,kickoff_at=kickoff,
                status='scheduled' if case=='pending' else 'finished',ft_home=2,ft_away=1,raw={})
            s.add(m);await s.flush()
            pick=dict(id=case,market='first_half_goals' if case=='awaiting' else 'result',
                selection='over_1_5' if case=='awaiting' else 'away' if case=='lost' else 'home',
                probability=.8,confidence='high',reliability=.8,sample_size=30,label=case)
            snapshot=PredictionSnapshot(match_id=m.id,league_id=league.id,captured_at=kickoff-timedelta(hours=1),model_version='isolated-test',picks=[pick])
            s.add(snapshot);await s.flush()
            if case in ('won','lost'): await settle_snapshot(s,snapshot,m,None)
        await s.commit()


async def test_today_separate_readonly_timezone_filters_and_idempotence(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await cohort(db,'settled');sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany): sql.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try:
        data=(await api.get('/api/performance-trends')).json()
        again=(await api.get('/api/performance-trends')).json()
    finally: event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert data==again
    assert len(sql)==4 and all(x.lstrip().startswith('SELECT') for x in sql)
    assert all('LIMIT' in x for x in sql[::2])
    assert data['today_excluded'] and data['comparisons'][0]['recent']['total']==0
    assert all(d['success_rate'] is None for d in data['daily'])
    today=data['current_day']
    assert today['date']=='2026-10-25' and today['total']==4
    assert (today['decided'],today['won'],today['lost'],today['pending'],today['awaiting_data'])==(2,1,1,1,1)
    assert today['success_rate']==.5
    filtered=(await api.get('/api/performance-trends?market=first_half_goals')).json()
    assert filtered['current_day']['total']==1 and filtered['current_day']['success_rate'] is None
    assert (await api.get('/api/performance-trends?date_from=2026-10-25')).json()['current_day']['total']==0
    assert (await api.get('/api/performance-trends?as_of=2026-10-24')).json()['current_day'] is None


@pytest.mark.parametrize('width',[390,1536])
@pytest.mark.parametrize('mode',['empty','pending','awaiting','settled','outage'])
async def test_current_and_empty_charts_chromium(api,db,monkeypatch,width,mode):
    import src.api.routes as routes
    import src.analytics.chart_diagnostics as diagnostics
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    monkeypatch.setattr(diagnostics,'utcnow',lambda:NOW)
    await cohort(db,'settled' if mode=='outage' else mode)
    before=os.getenv('CHART143_BEFORE')=='1'
    if before and mode!='settled':pytest.skip('One populated baseline capture per viewport')
    failures=mode=='outage';errors=[];calls=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960},reduced_motion='reduce')
        await page.clock.install(time=NOW)
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            assert route.request.url.startswith('http://test/') and route.request.method=='GET'
            path=route.request.url.removeprefix('http://test');calls.append(path)
            assert not any(x in path for x in ('telegram','nvidia'))
            if before and (path=='/' or path.endswith(('.css','.js'))):
                filename='dashboard.html' if path=='/' else path.lstrip('/')
                content=subprocess.check_output(['git','show','0300627:src/api/'+filename])
                await route.fulfill(body=content,content_type='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'application/javascript');return
            if failures and path.startswith('/api/performance-trends'):
                await route.fulfill(status=503,body='Unavailable');return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/')
            for name,chart_id,panel in [('home','home-results-chart','#home-daily-results'),('performance','trend-chart','.trend-panel')]:
                await page.locator(f'#primary-navigation [data-page-link={name}]').click()
                chart=page.locator('#'+chart_id);today=page.locator('#'+chart_id+'-current-day')
                if mode=='outage':
                    if name=='home': await expect(chart).to_contain_text('Veri bağlantısı bekleniyor')
                    else: await expect(page.locator('#trend-status')).to_contain_text('alınamadı')
                    await expect(chart.locator('svg')).to_have_count(0)
                    await expect(today).to_have_count(0)
                else:
                    await expect(chart.locator('svg')).to_have_count(1)
                    if not before:
                        await expect(today).to_contain_text('Bugün · 2026-10-25')
                        expected=4 if mode=='settled' else 0 if mode=='empty' else 1
                        await expect(today.locator('[data-count=total]')).to_have_attribute('data-value',str(expected))
                        await expect(today).to_contain_text('Başarı: 50%' if mode=='settled' else 'N/A')
                        await expect(chart.locator('[data-count=total]')).to_have_attribute('data-value','0')
                        assert await chart.locator('[data-series=empty-count]').count()==0
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if os.getenv('CHART143_SCREENSHOTS'):
                    folder=Path(os.environ['CHART143_SCREENSHOTS']);folder.mkdir(parents=True,exist_ok=True)
                    await page.add_style_tag(content='.terminal-header,.skip-link{visibility:hidden!important}')
                    await page.locator(panel).screenshot(path=str(folder/f'{"before" if before else "after"}-{name}-{mode}-{width}.png'))
                    await page.evaluate("document.querySelectorAll('style').forEach(e=>{if(e.textContent.includes('.terminal-header,.skip-link'))e.remove()})")
            if mode=='outage':
                failures=False
                await page.locator('#primary-navigation [data-page-link=home]').click()
                await page.locator('#home-results-retry').click()
                await expect(page.locator('#home-results-chart-current-day')).to_contain_text('Başarı: 50%')
                failures=True;await page.locator('#home-results-retry').click()
                await expect(page.locator('#home-results-chart-current-day')).to_have_count(0)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
