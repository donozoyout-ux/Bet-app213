"""Opt-in screenshots from real read-only API responses, never seeded display data."""
import json
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import httpx
import pytest
from playwright.async_api import async_playwright, expect


@pytest.mark.skipif(not os.getenv('CAPTURE_MINIMAL_ANALYSIS'),reason='Explicit live read-only visual capture')
async def test_capture_live_analysis(api):
    output=Path('docs/minimal-match-analysis');output.mkdir(parents=True,exist_ok=True)
    recorded={};checks=[]
    async with httpx.AsyncClient(base_url='https://bet-app213.onrender.com',timeout=45) as live:
        response=await live.get('/api/predictions?limit=8');response.raise_for_status()
        first=response.json();recorded[('GET','/api/predictions?limit=8')]=(200,first)
        assert first['cache']['stale'] is False
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            for baseline,width in [(True,1536),(True,390)]+[(False,w) for w in [320,390,768,1024,1536]]:
                page=await browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                await page.clock.install(time=datetime.fromisoformat(first['cache']['checked_at'].replace('Z','+00:00')) if first['cache'].get('checked_at') else datetime.now(timezone.utc))
                async def forward(route):
                    path=route.request.url.removeprefix('http://test');method=route.request.method
                    if baseline and (path=='/' or path.endswith('.js') or path.endswith('.css')):
                        filename='dashboard.html' if path=='/' else path.lstrip('/')
                        body=subprocess.check_output(['git','show','16d74ba:src/api/'+filename])
                        await route.fulfill(body=body,content_type='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'application/javascript');return
                    if path.startswith('/api/') or path=='/health':
                        if any(s in path for s in ['/telegram','/nvidia','/learning','/scrape','/coupons/automatic']):
                            await route.fulfill(status=503,json={'detail':'Not requested during read-only visual capture'});return
                        assert method=='GET' or path=='/api/coupon/quote'
                        key=(method,path)
                        if key not in recorded:
                            r=await live.request(method,path,content=route.request.post_data,
                                headers={'Content-Type':'application/json'} if method=='POST' else {})
                            recorded[key]=(r.status_code,r.json())
                        code,data=recorded[key];await route.fulfill(status=code,json=data);return
                    response=await api.get(path)
                    await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
                await page.route('http://test/**',forward)
                try:
                    await page.goto('http://test/#predictions')
                    if baseline:
                        await expect(page.locator('#market-picks tr[data-match-id]')).not_to_have_count(0,timeout=60000)
                    else:
                        await expect(page.locator('#match-analysis')).to_have_attribute('aria-busy','false',timeout=60000)
                        await expect(page.locator('[data-analysis-match]')).not_to_have_count(0,timeout=60000)
                        await expect(page.locator('[data-analysis-block]')).to_have_count(5,timeout=60000)
                        await page.locator('[data-analysis-block=main] > summary').focus()
                        await page.locator('[data-analysis-block=main] > summary').evaluate('(el)=>el.blur()')
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors
                    await page.screenshot(path=str(output/f'{"before" if baseline else "after"}-{width}.png'),full_page=True)
                    if not baseline and width in [390,1536]:
                        await page.locator('[data-analysis-block=source] > summary').click()
                        await page.screenshot(path=str(output/f'sources-{width}.png'),full_page=True)
                    checks.append({'baseline':baseline,'width':width,'overflow':False})
                finally:
                    await page.unroute_all(behavior='ignoreErrors');await page.close()
            await browser.close()
    evidence={'captured_at':datetime.now().astimezone().isoformat(),'basis':'Actual deployed API responses; no seeded matches, odds or samples',
        'prediction_generated_at':first['generated_at'],'cache':first['cache'],'matches':[
            {'id':i['match']['id'],'home':i['match']['home_team'],'away':i['match']['away_team'],
             'kickoff_at':i['match']['kickoff_at'],'status':i['match']['status']} for i in first['items']],
        'requests':[{'method':m,'path':path,'status':value[0]} for (m,path),value in recorded.items()], 'screens':checks}
    (output/'capture-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
