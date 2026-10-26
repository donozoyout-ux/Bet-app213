from datetime import timedelta
import os
from pathlib import Path
import pytest
from sqlalchemy import select, func
from src.models import Match, MatchStatistics, PredictionSnapshot, PredictionSettlement
from tests.test_predictions import seed, NOW


async def test_dossier_bounded_history_missing_data_and_no_retrospective_predictions(api, db):
    target, league, _, _, history = await seed(db)
    async with db.session() as session:
        match = await session.get(Match, target)
        match.status = 'finished'; match.ft_home = 2; match.ft_away = 1
        snapshot=await session.get(PredictionSnapshot,target)
        match.kickoff_at=snapshot.captured_at-timedelta(minutes=1)
        await session.commit()
    response = await api.get(f'/api/matches/{target}/dossier')
    assert response.status_code == 200
    data = response.json()
    assert data['predictions'] == [] and data['captured_at'] is None
    assert data['observations']['home_corners'] is None
    assert data['profiles']['home']['10']['corners'] == {'value':None,'sample':0}
    assert len(data['histories']['home']['items']) == 10
    assert data['histories']['home']['has_more']
    page = (await api.get(f'/api/matches/{target}/history?scope=home&offset=10')).json()
    assert not {m['id'] for m in page['items']} & {m['id'] for m in data['histories']['home']['items']}
    for suffix in ['limit=21','offset=1001','scope=unknown']:
        assert (await api.get(f'/api/matches/{target}/history?{suffix}')).status_code == 422
    assert (await api.get('/api/matches/9999999/dossier')).status_code == 404
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(PredictionSettlement)) == 0


async def test_dossier_only_displays_verified_recorded_results(api,db):
    from src.analytics.settlements import settle_match
    target,league,_,_,_=await seed(db)
    async with db.session() as session:
        m=await session.get(Match,target);m.status='finished';m.ft_home=2;m.ft_away=1
        await session.commit()
    data=(await api.get(f'/api/matches/{target}/dossier')).json()
    assert data['predictions'][0]['outcome']=='awaiting_data'
    async with db.session() as session:
        await settle_match(session,await session.get(Match,target));await session.commit()
    data=(await api.get(f'/api/matches/{target}/dossier')).json()
    assert data['predictions'][0]['outcome'] in ('won','lost')
    async with db.session() as session:
        m=await session.get(Match,target);m.ft_home=0;m.ft_away=0;await session.commit()
    data=(await api.get(f'/api/matches/{target}/dossier')).json()
    assert data['predictions'][0]['outcome']=='disputed'


@pytest.mark.parametrize('width',[390,1536])
async def test_match_page_chromium(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright, expect
    import src.api.match_page as dossier
    import src.analytics.settlements as settlements
    monkeypatch.setattr(dossier,'utcnow',lambda:NOW+timedelta(days=2))
    monkeypatch.setattr(settlements,'utcnow',lambda:NOW+timedelta(days=1,hours=3))
    target,_,_,_,history=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,target)
        match.status='finished';match.ft_home=2;match.ft_away=1;match.ht_home=1;match.ht_away=0
        match.status_observed_at=NOW+timedelta(days=1,hours=2)
        for match_id in history[:15]:
            session.add(MatchStatistics(match_id=match_id,is_final=True,home_corners=5,away_corners=3,
                home_yellow_cards=2,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,updated_at=NOW-timedelta(days=1)))
        session.add(MatchStatistics(match_id=target,is_final=True,home_corners=5,away_corners=3,
            home_yellow_cards=2,away_yellow_cards=1,home_red_cards=0,away_red_cards=0,
            updated_at=NOW+timedelta(days=1,hours=2)))
        await session.flush()
        from src.analytics.settlements import settle_match
        await settle_match(session,match)
        await session.commit()
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        context=await browser.new_context(viewport={'width':width,'height':960})
        requests=[]
        fail_once=True
        async def forward(route):
            nonlocal fail_once
            assert route.request.url.startswith('http://test/')
            assert route.request.method=='GET'
            requests.append(route.request.url)
            if fail_once and route.request.url.endswith('/dossier'):
                fail_once=False
                await route.fulfill(status=503,body='Unavailable')
                return
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await context.route('**/*',forward)
        page=await context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(f'http://test/match/{target}')
        await expect(page.locator('#feedback')).to_have_text('Veri bağlantısı kurulamadı.')
        await page.locator('#retry').click()
        await expect(page.locator('#hero h1')).to_have_text('Test team 0')
        for key in ['overview','odds','teams','counts','predictions']:
            await page.locator('#tab-'+key).click()
            await expect(page.locator('#panel-'+key)).to_be_visible()
            await page.locator('#panel-'+key).evaluate('async e=>{await Promise.all(e.getAnimations().map(a=>a.finished))}')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            if os.getenv('MATCH_SCREENSHOT_DIR'):
                folder=Path(os.environ['MATCH_SCREENSHOT_DIR']);folder.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(folder/f'{key}-{width}.png'),full_page=True)
        await page.locator('#tab-predictions').click()
        await expect(page.locator('#panel-predictions')).to_contain_text('Güncel öneriler')
        await expect(page.locator('#panel-predictions')).to_contain_text('İlk kaydedilen maç öncesi tahminler')
        await expect(page.locator('#panel-predictions')).to_contain_text('Başlamış veya bitmiş maç')
        await page.locator('#tab-predictions').press('Home')
        await expect(page.locator('#tab-overview')).to_have_text('Genel bakış')
        await expect(page.locator('#tab-overview')).to_be_focused()
        await page.keyboard.press('ArrowRight')
        await expect(page.locator('#tab-odds')).to_have_attribute('aria-selected','true')
        await page.locator('#tab-teams').click()
        await expect(page.locator('[data-team-period="10"]')).to_have_attribute('aria-pressed','true')
        await expect(page.locator('#panel-teams .compare-row')).to_have_count(28)
        await expect(page.locator('#h2h-evidence')).to_contain_text('Önceki derbiler')
        await expect(page.locator('#panel-teams .team-form-strip')).to_have_count(4)
        await expect(page.locator('#panel-teams .table-scroll')).to_have_count(5)
        await page.locator('[data-team-period="5"]').click()
        await expect(page.locator('[data-team-period="5"]')).to_have_attribute('aria-pressed','true')
        await expect(page.locator('.compare-head').first).to_contain_text('Son 5')
        await expect(page.locator('#panel-teams .compare-row')).to_have_count(28)
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        a=page.locator('#panel-teams a').first
        await expect(a).to_have_attribute('target','_blank')
        await expect(a).to_have_attribute('rel','noopener noreferrer')
        async with context.expect_page() as opened: await a.click()
        popup=await opened.value
        await popup.wait_for_load_state()
        assert '/match/' in popup.url
        assert await popup.evaluate('window.opener===null')
        await expect(popup.locator('#hero h1')).not_to_be_empty()
        await page.reload();await expect(page.locator('#panel-teams')).to_be_visible()
        await page.emulate_media(reduced_motion='reduce')
        assert await page.locator('#panel-teams').evaluate('e=>getComputedStyle(e).animationName')=='none'
        assert not errors
        assert not any('telegram' in r or 'nvidia' in r for r in requests)
        await page.goto('http://test/match/9999999')
        await expect(page.locator('#feedback')).to_have_text('Maç bulunamadı.')
        await expect(page.locator('#hero h1')).to_have_count(0)
        await browser.close()


@pytest.mark.parametrize('width',[390,1536])
async def test_dashboard_match_links_open_independent_tabs(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright, expect
    from tests.test_automatic_telegram import many_matches, NOW
    import src.api.routes as routes
    import src.api.match_page as dossier
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    monkeypatch.setattr(dossier,'utcnow',lambda:NOW)
    await many_matches(db,4)
    await api.post('/api/coupons/automatic')
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        context=await browser.new_context(viewport={'width':width,'height':960})
        async def forward(route):
            assert route.request.url.startswith('http://test/')
            path=route.request.url.removeprefix('http://test')
            assert not any(s in path for s in ('telegram/verify','nvidia'))
            response=await api.request(route.request.method,path,content=route.request.post_data,
                headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key')})
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await context.route('**/*',forward)
        page=await context.new_page();await page.clock.install(time=NOW)
        await page.goto('http://test/')
        for section in ['home','matches','predictions','markets','coupon']:
            await page.locator(f'#primary-navigation [data-page-link={section}]').click()
            if section=='markets': await page.locator('[data-market-family=first_half_goals]').click()
            a=page.locator(f'[data-page={section}] a[href^="/match/"]').first
            await expect(a).to_be_visible()
            await expect(a).to_have_attribute('rel','noopener noreferrer')
            url=await a.get_attribute('href');original=page.url
            await a.focus()
            async with context.expect_page() as opened: await page.keyboard.press('Enter')
            popup=await opened.value
            await expect(popup.locator('#hero h1')).not_to_be_empty()
            assert popup.url.startswith('http://test'+url)
            assert await popup.evaluate('window.opener===null')
            assert page.url==original
            await popup.close()
        await browser.close()


async def test_match_dossier_separates_current_board_from_first_saved_picks(api, db, monkeypatch):
    """An evolving homepage recommendation never overwrites the first archived picks."""
    from sqlalchemy.orm.attributes import flag_modified
    from src.models import PredictionBoard
    import src.api.match_page as dossier
    monkeypatch.setattr(dossier, 'utcnow', lambda: NOW + timedelta(minutes=1))
    target, league, _, _, _ = await seed(db)
    async with db.session() as session:
        snapshot = await session.get(PredictionSnapshot, target)
        original = list(snapshot.picks)
        board = await session.get(PredictionBoard, league)
        assert board is not None
        payload = dict(board.payload)
        items = []
        inserted = False
        for item in payload['items']:
            if item['match']['id'] != target:
                items.append(item)
                continue
            new_pick = {
                'id': 'over_7_5_corners', 'label': '7.5 Korner Üst',
                'selection': 'over_7_5', 'market': 'corners',
                'probability': 0.838845, 'reliability': 0.91,
                'sample_size': 29, 'confidence': 'medium'
            }
            items.append({**item, 'recommendations': [new_pick]})
            inserted = True
        assert inserted
        board.payload = {**payload, 'items': items}
        flag_modified(board, 'payload')
        await session.commit()

    result = await api.get(f'/api/matches/{target}/dossier')
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['current_recommendations'][0]['label'] == '7.5 Korner Üst'
    assert data['current_generated_at'] is not None
    assert data['current_expires_at'] is not None
    assert data['predictions']
    assert [item['pick'] for item in data['predictions']] == original
    assert data['captured_at'] is not None
    assert 'ana sayfa' in data['current_note'].lower()

    # Stale publications are not mislabeled as today's current predictions.
    monkeypatch.setattr(dossier, 'utcnow', lambda: NOW + timedelta(minutes=20))
    expired = (await api.get(f'/api/matches/{target}/dossier')).json()
    assert expired['current_recommendations'] == []
    assert expired['predictions']
    # Finished fixtures must never acquire a retrospective 'current' pick.
    async with db.session() as session:
        match = await session.get(Match, target)
        match.status = 'finished'
        match.ft_home = 1
        match.ft_away = 0
        await session.commit()
    finished = (await api.get(f'/api/matches/{target}/dossier')).json()
    assert finished['current_recommendations'] == []
    assert [item['pick'] for item in finished['predictions']] == original


@pytest.mark.parametrize('width', [390, 1536])
async def test_match_current_board_diff_browser_without_rewriting_archive(api, db, width):
    from playwright.async_api import async_playwright, expect
    import json
    target, _, _, _, _ = await seed(db)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={'width': width, 'height': 850})
        async def forward(route):
            path = route.request.url.removeprefix('http://test')
            response = await api.get(path)
            if path.endswith('/dossier') and response.status_code == 200:
                payload = response.json()
                payload['current_recommendations'] = [
                    {'id': 'over_7_5_corners', 'label': '7.5 Korner Üst',
                     'probability': .838845, 'reliability': .911875, 'sample_size': 29}]
                payload['current_generated_at'] = '2026-10-26T17:10:21Z'
                payload['current_expires_at'] = '2026-10-26T17:20:21Z'
                payload['current_note'] = 'Ana sayfa ile aynı güncel lig yayını.'
                await route.fulfill(status=200, body=json.dumps(payload), content_type='application/json')
            else:
                await route.fulfill(status=response.status_code, body=response.content,
                                    content_type=response.headers.get('content-type', 'application/json'))
        await context.route('**/*', forward)
        page = await context.new_page()
        await page.goto(f'http://test/match/{target}#predictions')
        await expect(page.locator('#panel-predictions .current-pick h3')).to_have_text('7.5 Korner Üst')
        await expect(page.locator('#panel-predictions')).to_contain_text('İlk kaydedilen maç öncesi tahminler')
        await expect(page.locator('#panel-predictions')).to_contain_text('İlk kaydedilmiş tahmin setinde yok')
        await expect(page.locator('#panel-predictions')).to_contain_text('İlk kayıt:')
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await browser.close()
