"""Learning UI acceptance against persisted editions and the real ASGI schema."""
import json
import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest
from playwright.async_api import async_playwright, expect
from src.models import LearningReport, LearningHead, LearningLesson, LearningSlice, utcnow
from src.analytics.learning_metrics import METHOD


async def edition(db, *, empty=False, fingerprint=None, title=None):
    data=json.loads(Path('docs/learning-phase-2-example.json').read_text(encoding='utf-8'))
    async with db.session() as session:
        report=LearningReport(method_version=METHOD, data_fingerprint=fingerprint or data['data_fingerprint'],
            source_observed_at=utcnow()-timedelta(hours=2), source_manifest=[],
            summary=data['summary'],coverage=data['coverage'])
        session.add(report);await session.flush()
        if not empty:
            for i,row in enumerate(data['lessons']):
                payload=deepcopy(row['payload'])
                if i==0:payload['title']=title or '<img src=x onerror="window.pwned=true">'
                session.add(LearningLesson(report_id=report.id,scope_key=row['scope_key'],payload=payload))
            for row in data['slices']:
                session.add(LearningSlice(report_id=report.id,**row))
        head=await session.get(LearningHead,METHOD)
        if head:head.report_id=report.id;head.source_observed_at=report.source_observed_at
        else:session.add(LearningHead(method_version=METHOD,report_id=report.id,source_observed_at=report.source_observed_at))
        await session.commit();return report.id


@pytest.mark.parametrize('width',[320,390,768,1024,1536])
async def test_learning_navigation_schema_filters_and_safe_evidence(api,db,width):
    report=await edition(db)
    calls=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':960},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#performance')
            nav='#primary-navigation'
            await page.locator(nav+' [data-page-link=lessons]').click()
            await expect(page.locator('#learning-page')).to_contain_text('55 kayıt',timeout=15000)
            await expect(page.locator('#learning-status')).to_contain_text('Eski rapor')
            await expect(page.locator('#learning-summary')).to_contain_text('280')
            await expect(page.locator('#learning-summary')).to_contain_text('76,7')
            await expect(page.locator('.learning-card')).to_have_count(10)
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(target/f'learning-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('#learning-next').click()
            await expect(page.locator('#learning-page')).to_contain_text('Sayfa 2')
            await page.locator('#learning-kind').select_option('overall')
            await expect(page.locator('.learning-card')).to_have_count(1)
            await expect(page.locator('.learning-card h2')).to_have_text('<img src=x onerror="window.pwned=true">')
            assert await page.locator('.learning-card img').count()==0
            assert await page.evaluate('window.pwned') is None
            await expect(page.locator('.learning-evidence')).to_contain_text('doğrulanmış bir ders değildir')
            summary=page.locator('.learning-card > details:not(.learning-ai) summary');await summary.focus();await page.keyboard.press('Enter')
            await expect(page.locator('.learning-card > details:not(.learning-ai)')).to_have_attribute('open','')
            await expect(page.locator('.learning-card > details:not(.learning-ai)')).to_contain_text(METHOD)
            await expect(page.locator('.learning-card > details:not(.learning-ai)')).to_contain_text('güven aralığı')
            for kind in ('league','market','model'):
                await page.locator('#learning-kind').select_option(kind)
                values=await page.locator('#learning-scope option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')
                assert len(values)>1
                await page.locator('#learning-scope').select_option(values[1])
                await expect(page.locator('.learning-card')).to_have_count(1)
                await expect(page.locator('#learning-statistics')).to_contain_text('Brier')
            await page.go_back();await expect(page.locator('[data-page=performance]')).to_be_visible()
            await page.go_forward();await expect(page.locator('[data-page=lessons]')).to_be_visible()
            pinned=[path for path in calls if path.startswith(('/api/learning/lessons?','/api/learning/statistics?','/api/learning/coverage?'))]
            assert len(pinned)>=5
            assert all(parse_qs(urlparse(path).query)['report_id']==[str(report)] for path in pinned)
            assert not errors
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_learning_warming_empty_failure_retry_and_report_switch(api,db):
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        fail=False
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if fail and path.startswith('/api/learning/lessons'):
                await route.fulfill(status=503,json={'detail':'Unavailable'});return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#lessons')
            await expect(page.locator('#learning-status')).to_contain_text('İlk öğrenme raporu hazırlanıyor')
            old=await edition(db,empty=True,fingerprint='a'*64)
            await page.locator('#learning-refresh').click()
            await expect(page.locator('#learning-lessons')).to_contain_text('kaydı bulunmuyor')
            new=await edition(db)
            fail=True;await page.locator('#learning-refresh').click()
            await expect(page.locator('#learning-status')).to_contain_text('alınamadı')
            await expect(page.locator('.learning-card')).to_have_count(0)
            fail=False;await page.locator('#learning-refresh').click()
            await expect(page.locator('#learning-page')).to_contain_text('55 kayıt')
            await page.locator('#learning-report').select_option(str(old))
            await expect(page.locator('#learning-status')).to_contain_text('Arşiv raporu')
            await expect(page.locator('#learning-lessons')).to_contain_text('kaydı bulunmuyor')
            await page.locator('#learning-report').select_option(str(new))
            await expect(page.locator('#learning-page')).to_contain_text('55 kayıt')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()

async def test_learning_server_pagination_and_loading_are_pinned(api,db):
    import asyncio
    report=await edition(db)
    # Exceed the endpoint page size while retaining the actual persisted schema.
    async with db.session() as session:
        for i in range(105):
            session.add(LearningLesson(report_id=report,scope_key=f'model:extra-{i:03}',payload=dict(
                title=f'Yeni gözlem {i}',date='2026-10-16T10:00:00Z',model_version=f'extra-{i}',
                status='inconclusive',confidence='low',predictions=2,matches=1,
                expected_success=.8,observed_success=.5,evidence={})))
        await session.commit()
    started=asyncio.Event();release=asyncio.Event();calls=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        async def forward(route):
            path=route.request.url.removeprefix('http://test');calls.append(path)
            if path.startswith('/api/learning/lessons') and 'offset=0' in path:
                started.set();await release.wait()
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#lessons',wait_until='domcontentloaded')
            await asyncio.wait_for(started.wait(),10)
            await expect(page.locator('#learning-status')).to_contain_text('yükleniyor')
            await expect(page.locator('#learning-lessons')).to_have_attribute('aria-busy','true')
            # Publish another edition mid-load: every remaining page must use the original.
            await edition(db,empty=True,fingerprint='b'*64)
            release.set()
            await expect(page.locator('#learning-page')).to_contain_text('160 kayıt',timeout=15000)
            await expect(page.locator('.learning-card h2').first).to_have_text('Yeni gözlem 0')
            assert any('offset=100' in path and f'report_id={report}' in path for path in calls)
            await page.locator('#learning-kind').select_option('model')
            await expect(page.locator('#learning-page')).to_contain_text('Sayfa 1')
            await page.locator('#learning-refresh').click()
            await expect(page.locator('#learning-lessons')).to_contain_text('kaydı bulunmuyor')
        finally:
            release.set();await page.unroute_all(behavior='ignoreErrors');await browser.close()

@pytest.mark.parametrize('width',[320,768,1536])
async def test_ai_explanation_real_api_background_and_disabled_states(api,db,monkeypatch,width):
    import httpx
    from src.analytics import learning_ai as ai
    from tests.test_learning_ai import CFG,SECRET,success,only_general
    await only_general(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':960})
        calls=[]
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            assert 'nvidia.com' not in path
            response=await api.get(path);assert SECRET not in response.text
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('**/*',forward)
        try:
            await page.goto('http://test/#lessons')
            await expect(page.locator('.learning-ai')).to_have_count(0)
            await expect(page.locator('#learning-coverage')).to_contain_text('AI anlatımı hazır değil')
            monkeypatch.setattr(ai,'config',lambda:CFG)
            await page.locator('#learning-refresh').click()
            await expect(page.locator('#learning-lessons')).to_have_attribute('aria-busy','false')
            await expect(page.locator('.learning-ai')).to_have_count(0)
            await expect(page.locator('#learning-coverage')).to_contain_text('AI anlatımı hazır değil')
            def provider(request):calls.append(request);return success(request)
            assert not calls
            assert await ai.run_once(db,CFG,httpx.MockTransport(provider))=='completed'
            # Simulate the regular page tick, not a generation endpoint or forced reload.
            await page.evaluate('BetAppLearning.refresh()')
            await page.locator('.learning-ai > summary').click()
            await expect(page.locator('.learning-ai')).to_contain_text('Dayanak: Rapor #')
            await expect(page.locator('.learning-ai')).to_contain_text('120 tahmin / 61 maç')
            await expect(page.locator('.learning-ai')).to_contain_text('doğrulanmış bir ders değildir')
            if os.getenv('UI_SCREENSHOT_DIR'):
                target=Path(os.environ['UI_SCREENSHOT_DIR']);target.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(target/f'learning-ai-ready-{width}.png'),full_page=True)
            assert len(calls)==1
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert SECRET not in await page.content()
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
