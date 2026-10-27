"""Browser coverage navigation uses source scopes, never secrets or fake live data."""
from pathlib import Path
import json
import pytest
from playwright.async_api import async_playwright,expect


@pytest.mark.parametrize('verified',[False,True])
async def test_dashboard_coverage_and_scoped_match_links(verified):
    generation='a'*64
    scope='/api/matches?history_source=archive&history_generation='+generation
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page();errors=[];requests=[]
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        async def respond(route):
            url=route.request.url;requests.append(url)
            assert url.startswith('http://test/') and route.request.method=='GET'
            if url=='http://test/':
                await route.fulfill(body='<html><head><meta charset="utf-8"></head><body><script src="/dashboard-history.js"></script></body></html>',content_type='text/html')
            elif url.endswith('/dashboard-history.js'):
                await route.fulfill(body=Path('src/api/dashboard-history.js').read_text(encoding='utf-8'),content_type='application/javascript')
            elif url.endswith('/api/history/coverage'):
                data={'status':'verified' if verified else 'disabled','covered':1,'retained':2,
                    'capacity':{'blocked':False},'partitions':[{'generation':generation,'matches':1,'seasons':['fixture-only'],'href':scope}] if verified else []}
                await route.fulfill(body=json.dumps(data),content_type='application/json')
            elif '/api/matches?' in url:
                assert 'history_source=archive' in url and 'history_generation='+generation in url
                await route.fulfill(body=json.dumps({'items':[{'id':7,'home_team':'Fixture A','away_team':'Fixture B'}],'total':1}),content_type='application/json')
            else:raise AssertionError(url)
        await page.route('**/*',respond)
        await page.goto('http://test/')
        panel=page.locator('#history-archive-coverage')
        if verified:
            await expect(panel).to_contain_text('Doğrulanmış arşiv: 1 maç')
            await panel.locator('summary').click()
            await panel.get_by_role('button').click()
            link=panel.get_by_role('link',name='Fixture A — Fixture B')
            await expect(link).to_have_attribute('href','/match/7?history_source=archive&history_generation='+generation)
        else:
            await expect(panel).to_contain_text('üretim arşivi etkin değil')
            assert not any('/api/matches?' in u for u in requests)
        assert not errors
        await browser.close()
