from pathlib import Path
from playwright.sync_api import sync_playwright


def test_history_page_renders_and_reports_outage_without_html_execution():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page()
        html=Path('src/api/history-archive.html').read_text(encoding='utf-8')
        def route(request):
            if '/api/history/archive' in request.request.url:
                request.fulfill(json=dict(total=1,snapshot='a'*64,items=[dict(
                    match=dict(home_team='<img src=x onerror=alert(1)>',away_team='Away',
                               ft_home=1,ft_away=0,odds=[]),day='2026-10-30',revision=2,
                    evidence=[dict(outcome='won')])]))
            else: request.fulfill(body=html,content_type='text/html')
        page.route('**/*',route)
        page.goto('http://archive.test/history-archive')
        page.wait_for_selector('article')
        assert page.locator('article h2').inner_text() == '<img src=x onerror=alert(1)> – Away'
        assert page.locator('article img').count() == 0
        assert page.locator('#next').is_disabled()
        page.unroute('**/*')
        page.route('**/*',lambda request:request.fulfill(status=503,body='unavailable'))
        page.locator('#filters button').click()
        page.wait_for_function("document.getElementById('status').textContent.includes('doğrulanamıyor')")
        assert page.locator('article').count() == 0
        browser.close()
