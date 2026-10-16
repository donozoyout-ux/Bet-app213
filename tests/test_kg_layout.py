"""KG wiring, immutable accounting and bounded detail revision regressions."""
from copy import deepcopy
import pytest
from sqlalchemy import event
from src.models import Match, PredictionSnapshot
from src.analytics.service import revision
from tests.test_predictions import seed, NOW


async def test_detail_revision_prices_are_scoped_but_history_is_global(db):
    target,*_=await seed(db)
    queries=[]
    def trace(conn,cursor,statement,parameters,context,executemany):
        queries.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:
        async with db.session() as session:
            scoped=await revision(session,target)
            global_revision=await revision(session)
        assert len(scoped)==len(global_revision)==13
        assert queries[0].count('match_id =')==4
        assert 'WHERE' not in queries[1]
    finally:
        event.remove(db.engine.sync_engine,'before_cursor_execute',trace)


async def test_performance_deduplicates_same_selection_and_accounts_all_outcomes(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,*_=await seed(db)
    pick=dict(id='yes_btts',market='btts',selection='yes',label='KG Var',probability=.7,
              reliability=.8,score=.8,sample_size=10,confidence='high')
    async with db.session() as session:
        original=await session.get(Match,target)
        match=Match(external_match_id=998877,league_id=original.league_id,season_id=original.season_id,
            home_team_id=original.home_team_id,away_team_id=original.away_team_id,
            round=1,status='finished',ft_home=1,ft_away=0,kickoff_at=original.kickoff_at,raw={})
        session.add(match);await session.flush()
        snapshot=PredictionSnapshot(match_id=match.id,league_id=match.league_id,
            captured_at=NOW,model_version='test',picks=[pick,deepcopy(pick),dict(pick,id='no_btts',selection='no',label='KG Yok')])
        session.add(snapshot)
        await session.commit()
    data=(await api.get('/api/market-performance?market=btts')).json()
    s=data['summary']
    assert data['duplicate_picks_excluded']==1
    assert data['total']==s['total']==2
    assert s['total']==s['won']+s['lost']+s['pending']+s['awaiting_data']+s['void']
    assert s['settled']==s['won']+s['lost']==2
    assert s['success_rate']==.5
    assert {i['recommendation']['selection']:i['outcome'] for i in data['items']}=={'yes':'lost','no':'won'}



def test_kg_never_uses_totals_or_result_prices():
    from src.analytics.odds_matching import match_odds_for_candidate
    from types import SimpleNamespace
    source=SimpleNamespace(match_id=1,latest_home=1.8,latest_over=1.8,latest_under=2.0,latest_line=2.5,updated_at=NOW)
    for side in ('yes','no'):
        result=match_odds_for_candidate(dict(market='btts',selection=side,probability=.7),1,'scheduled',
            odds_1x2_rows=[(source,'Crown')],asian_totals_rows=[(source,'Bet365')],cutoff=NOW)
        assert result['has_odds'] is False
        assert result.get('coupon_eligible') is not True
        assert result['reason']=='source_market_unavailable'


@pytest.mark.parametrize('width',[320,390,768,1024,1536])
async def test_kg_detail_tab_observations_and_responsive(api,db,monkeypatch,width):
    from playwright.async_api import async_playwright,expect
    from tests.test_minimal_analysis import fixtures,proxy
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,1)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':900})
        await page.clock.install(time=NOW)
        await page.route('http://test/**',lambda route:proxy(route,api))
        try:
            await page.goto('http://test/#predictions')
            await page.evaluate('(id)=>BetAppPredictions.select(id)',target)
            await page.locator('#analysis-tab-btts').click()
            box=page.locator('#statistics-detail')
            for text in ['KG Var','KG Yok','Son 5','Son 10','Atılan / yenilen','H2H KG Var','Crown','Bet365','Sbobet']:
                await expect(box).to_contain_text(text)
            await expect(box).not_to_contain_text('En İyi Oran')
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await expect(page.locator('[data-analysis-tab]')).to_have_count(11)
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_detail_504_is_distinct_from_stale_evidence(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    from tests.test_minimal_analysis import fixtures,proxy
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,1)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        await page.clock.install(time=NOW)
        async def forward(route):
            if route.request.url.endswith(f'/api/matches/{target}/statistics'):
                await route.fulfill(status=504,json={'detail':'test timeout'});return
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions')
            await expect(page.locator('#analysis-blocks')).to_contain_text('API 504')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()


async def test_board_display_query_does_not_load_coupon_context(api,db,monkeypatch):
    from tests.test_minimal_analysis import fixtures
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await fixtures(db,1)
    statements=[]
    def trace(conn,cursor,statement,parameters,context,executemany):
        statements.append((statement,parameters))
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:
        response=await api.get('/api/predictions?limit=8')
        assert response.status_code==200 and response.json()['items']
        board_sql=[q for q,args in statements if 'prediction_boards' in q]
        assert len(board_sql)==2  # validity metadata, then a cold publication projection
        assert ', prediction_boards.payload,' not in board_sql[0]
        assert all('coupon_records' not in str(args) for q,args in statements)
        diagnostic=await api.get('/api/odds-coverage')
        assert diagnostic.status_code==200
        assert diagnostic.json()['engine_analysis']
    finally:
        event.remove(db.engine.sync_engine,'before_cursor_execute',trace)


async def test_home_model_distinguishes_expired_analysis_and_connection_error(api):
    from playwright.async_api import async_playwright,expect
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page()
        async def forward(route):
            path=route.request.url.removeprefix('http://test')
            if path.startswith('/api/') or path=='/health':
                await route.fulfill(status=503,json={'detail':'isolated test'});return
            response=await api.get(path)
            await route.fulfill(status=response.status_code,body=response.content,
                content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#home')
            await page.evaluate("BetAppHome.renderModel({items:[],cache:{stale:false,expires_at:'2020-01-01T00:00:00Z'}})")
            await expect(page.locator('#home-model-body')).to_contain_text('Analiz güncel değil')
            await page.evaluate("BetAppHome.renderModel({items:[],error:'API 504 zaman aşımı'})")
            await expect(page.locator('#home-model-body')).to_contain_text('API 504')
            await page.evaluate('BetAppHome.renderModel({items:[],cache:{stale:false}})')
            await expect(page.locator('#home-model-body')).to_contain_text('Kalite eşiklerini')
        finally:
            await page.unroute_all(behavior='ignoreErrors');await browser.close()
