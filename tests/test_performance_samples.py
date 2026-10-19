"""Synthetic presentation examples; no production predictions or database writes."""
import os
from pathlib import Path
import pytest
from playwright.async_api import async_playwright, expect
from src.analytics.performance import tally, add, finish, wilson_interval

CASES=[(0,1),(1,1),(2,2),(3,4),(18,19),(15,20),(36,43),(22,27),(0,0)]

def examples():
    rows=[];overall=tally()
    for i,(won,n) in enumerate(CASES):
        counts=tally()
        pick=dict(reliability=.9,sample_size=30,probability=.8)
        for outcome in ['won']*won+['lost']*(n-won)+['pending','awaiting_data','disputed','void']:
            add(counts,pick,outcome);add(overall,pick,outcome)
        rows.append(dict(id=str(i),label=f'Example {i}',market='goals',**finish(counts)))
    return rows,finish(overall)

@pytest.mark.parametrize('won,n',CASES)
def test_sample_tier_denominator_and_interval(won,n):
    counts=tally();counts.update(total=n+100,settled=n,won=won,lost=n-won,pending=98,disputed=1,awaiting_data=1)
    result=finish(counts)
    assert result['sample_tier']==('comparable' if n>=20 else 'insufficient')
    assert result['success_rate']==(won/n if n else None)
    if n:
        assert 0<=result['wilson_95']['lower']<=won/n<=result['wilson_95']['upper']<=1
    else:assert result['wilson_95'] is None


def test_micro_is_weighted_and_small_losing_market_is_kept():
    rows,total=examples()
    assert total['won']==sum(w for w,n in CASES)
    assert total['settled']==sum(n for w,n in CASES)
    assert total['success_rate']==sum(w for w,n in CASES)/sum(n for w,n in CASES)
    assert total['success_rate']!=sum(r['success_rate'] for r in rows if r['settled'])/8
    assert wilson_interval(1,1)['lower']==pytest.approx(.2065493144)
    assert wilson_interval(0,1)['upper']==pytest.approx(.7934506856)

@pytest.mark.parametrize('width',[390,1536])
async def test_market_evidence_screen(api,width):
    rows,summary=examples();calls=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            response=await api.get(path)
            if path.startswith('/api/market-performance'):
                payload=response.json();payload.update(markets=rows,summary=summary,total=60)
                await route.fulfill(json=payload);return
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            await expect(page.locator('#performance-markets .sample-comparable')).to_have_count(3)
            await expect(page.locator('#performance-markets .sample-insufficient')).to_have_count(6)
            ordered=await page.locator('#performance-markets tr:not(.sample-group) td:first-child').all_text_contents()
            assert ordered[:3]==['Example 6','Example 7','Example 5']
            assert set(ordered)=={f'Example {i}' for i in range(9)}
            await expect(page.locator('#performance-summary')).to_contain_text(f"({summary['won']}/{summary['settled']})")
            # Model historical sample is 30, but a singleton remains insufficient.
            await expect(page.locator('.sample-insufficient').filter(has_text='Example 1')).to_contain_text('30.0')
            await page.locator('#performance-next').click()
            await expect(page.locator('#performance-note')).to_contain_text('Sayfa 2')
            assert any('offset=50' in path for path in calls)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.evaluate('window.scrollTo(0,0)')
                await page.screenshot(path=str(target/f'samples-{width}.png'),full_page=True,animations='disabled')
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
