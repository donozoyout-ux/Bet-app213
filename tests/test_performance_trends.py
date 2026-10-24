from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import pytest
from playwright.async_api import async_playwright, expect
from src.analytics.performance_trends import summarize, build_trends, ISTANBUL
from tests.test_performance_status import fixture

AS_OF=date(2026,10,19)

def rows(n,won,day,offset=0,version='v1'):
    return [dict(day=day,match_id=offset+i,league='Test League',market='goals',model_version=version,probability=.8,outcome='won' if i<won else 'lost') for i in range(n)]

@pytest.mark.parametrize('n,expected',[(29,False),(30,True)])
def test_sample_boundary(n,expected):
    assert summarize(rows(n,n,AS_OF-timedelta(days=1)))['sufficient'] is expected

@pytest.mark.parametrize('old,new,sign',[(10,25,1),(25,10,-1),(20,20,0)])
def test_nonoverlapping_comparison_and_no_learning_claim(old,new,sign):
    data=rows(30,old,AS_OF-timedelta(days=8))+rows(30,new,AS_OF-timedelta(days=1),100,version='v2')
    report=build_trends(data,AS_OF);c=report['comparisons'][0]
    assert c['recent']['decided']==c['previous']['decided']==30
    assert c['raw_difference']==pytest.approx((new-old)/30)
    assert c['eligible'] and not report['improvement_proven'] and not report['model_update_verified']
    assert report['model_versions']==['v1','v2']
    assert c['previous']['end_exclusive']==c['recent']['start']
    assert len(report['daily'])==60
    assert report['daily'][0]['success_rate'] is None


def test_cluster_dependence_incomplete_and_probability_valid_denominator():
    data=rows(30,24,AS_OF-timedelta(days=1))
    for r in data:r['match_id']=1
    summary=summarize(data)
    assert summary['distinct_matches']==summary['effective_matches']==1
    assert not summary['sufficient'] and summary['chart_rate']==.8
    assert summary['brier_score']==pytest.approx(.16)
    assert summary['mean_probability']==pytest.approx(.8)
    data[0]['probability']=None
    for outcome in ('pending','awaiting_data','disputed','void'):
        data.append(dict(data[-1],outcome=outcome))
    summary=summarize(data)
    assert summary['decided']==30 and summary['probability_valid']==29
    assert summary['disputed']==summary['awaiting_data']==summary['pending']==summary['void']==1
    assert summary['incomplete']
    assert summarize([])['brier_score'] is None


def test_timezone_midnight_delayed_settlement_truncation_and_date_anchor():
    day=datetime(2026,10,17,21,30,tzinfo=timezone.utc).astimezone(ISTANBUL).date()
    assert day==date(2026,10,18)
    data=rows(30,30,day)
    for r in data:r['recorded_at']='2026-10-30' # ignored: delayed settlement cannot shift cohort
    report=build_trends(data,AS_OF)
    assert report['daily'][-1]['decided']==30
    assert build_trends(data,day)['comparisons'][0]['recent']['decided']==0
    limited=build_trends(data,AS_OF,truncated=True)
    assert not limited['comparisons'][0]['eligible'] and all(d['chart_rate'] is None for d in limited['daily'])


async def test_readonly_endpoint_filters_and_model_versions(api,db):
    await fixture(db)
    response=await api.get('/api/performance-trends?as_of=2026-10-10')
    assert response.status_code==200
    data=response.json();assert 'test' in data['model_versions']
    assert data['comparisons'][0]['recent']['disputed']==1
    filtered=(await api.get('/api/performance-trends?as_of=2026-10-10&exact_market=0')).json()
    assert filtered['comparisons'][0]['recent']['decided']==1
    empty=(await api.get('/api/performance-trends?as_of=2026-10-01')).json()
    assert empty['comparisons'][0]['recent']['total']==0
    assert (await api.get('/api/performance-trends?as_of=2999-01-01')).status_code==400

@pytest.mark.parametrize('width',[390,1536])
async def test_real_api_trend_mobile_and_desktop(api,db,width):
    await fixture(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            await expect(page.locator('#trend-status')).to_have_text('İyileşme kanıtlanmadı')
            await page.locator('#trend-as-of').fill('2026-10-10')
            await page.locator('#trend-as-of').dispatch_event('change')
            await expect(page.locator('#trend-model')).to_contain_text('2026-10-10 öncesi')
            await expect(page.locator('#trend-model')).to_contain_text('Model değişikliği doğrulanmadı')
            await expect(page.locator('#trend-periods')).to_contain_text('7 gün / önceki 7 gün')
            await page.locator('#trend-window').select_option('30')
            await expect(page.locator('.trend-period:visible')).to_contain_text('30 gün / önceki 30 gün')
            await expect(page.locator('.trend-period:visible .trend-bar')).to_have_count(2)
            await page.locator('#trend-window').select_option('7')
            await expect(page.locator('.trend-period:visible')).to_contain_text('7 gün / önceki 7 gün')
            await expect(page.locator('#trend-chart')).to_contain_text('Örneklem düşük')
            await expect(page.locator('#trend-chart circle')).to_have_count(1)
            await page.locator('#trend-chart circle').focus()
            await expect(page.locator('.trend-tooltip')).to_contain_text('Sonuçlanan 2 · Kazandı 1 · Kaybetti 1')
            await expect(page.locator('.trend-tooltip')).to_contain_text('Veri eksik 1 · İncelenmeli 1')
            await expect(page.locator('.trend-tooltip')).to_contain_text('Brier 0.340')
            await expect(page.locator('.trend-period:visible .trend-bar')).to_have_count(2)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.locator('#trend-chart circle').blur()
                await page.add_style_tag(content='.bottom-nav,.terminal-header,.skip-link,.floating-tools{visibility:hidden!important}')
                await page.locator('.trend-panel').screenshot(path=str(target/f'trends-{width}.png'))
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_bounded_two_read_queries_per_uncached_request(api,db):
    from sqlalchemy import event
    await fixture(db);statements=[]
    def collect(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',collect)
    try:
        first=await api.get('/api/performance-trends?as_of=2026-10-11')
        assert first.status_code==200
        assert len(statements)==2
        assert all(s.lstrip().upper().startswith('SELECT') for s in statements)
        assert 'LIMIT' in statements[0] and 'kickoff_at >=' in statements[0]
        await api.get('/api/performance-trends?as_of=2026-10-11')
        # SQLite intentionally bypasses the production read cache.
        assert len(statements)==4
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',collect)


async def test_chart_gaps_and_descriptive_increase(api):
    report=build_trends(rows(30,0,AS_OF-timedelta(days=8))+rows(30,30,AS_OF-timedelta(days=1),100),AS_OF)
    assert report['comparisons'][0]['direction']=='increase'
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':390,'height':960})
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/performance-trends'):
                import json
                await route.fulfill(content_type='application/json',body=json.dumps(report,default=str));return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            await expect(page.locator('#trend-chart circle')).to_have_count(2)
            # Only vertical uncertainty whiskers: no line across seven missing days.
            await expect(page.locator('#trend-chart path[stroke="#38bdf8"]')).to_have_count(1)
            await expect(page.locator('#trend-status')).to_have_text('İyileşme kanıtlanmadı')
            await expect(page.locator('#trend-periods')).to_contain_text('Artış sinyali; kalıcılık/öğrenme kanıtı değil')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


def test_weekly_weighted_aggregation_and_daily_density():
    data=rows(1,1,date(2026,10,13))+rows(3,0,date(2026,10,14),10)
    report=build_trends(data,AS_OF)
    assert report['aggregation']=='weekly'
    week=report['weekly'][-1]
    assert week['date']=='2026-10-12' and week['end_exclusive']=='2026-10-19'
    assert week['decided']==4 and week['chart_rate']==.25
    assert week['brier_score']==pytest.approx(.49)
    dense=[r for i in range(14) for r in rows(5,3,AS_OF-timedelta(days=i+1),i*10)]
    assert build_trends(dense,AS_OF)['aggregation']=='daily'


async def test_no_decided_empty_state_and_capture_date_filters(api,db):
    await fixture(db)
    data=(await api.get('/api/performance-trends?as_of=2026-10-10&date_from=2026-10-09')).json()
    assert not any(p['decided'] for p in data['daily'])
    assert (await api.get('/api/performance-trends?date_from=2026-10-10&date_to=2026-10-01')).status_code==400
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            await page.locator('#trend-as-of').fill('2026-10-01')
            await page.locator('#trend-as-of').dispatch_event('change')
            await expect(page.locator('#trend-chart')).to_contain_text('Henüz sonuçlanan tahmin yok')
            await expect(page.locator('#trend-chart circle')).to_have_count(0)
            await expect(page.locator('#trend-chart svg')).to_have_count(0)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
