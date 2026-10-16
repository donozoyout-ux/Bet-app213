"""A reload or match selection must never execute a prediction engine."""
from datetime import timedelta
from copy import deepcopy
import pytest
from sqlalchemy import select,func,event
from src.models import PublishedMatchAnalysis,Match,BoardHead
from tests.test_predictions import seed,NOW


async def test_same_evidence_renews_lease_without_payload_rewrite(db,monkeypatch):
    from src.analytics.board_cache import refresh_one
    import src.analytics.service as service
    target,league,*_=await seed(db)
    async with db.session() as s:
        first=await s.get(PublishedMatchAnalysis,target);version=first.version;generated=first.generated_at
        board_version=(await s.get(BoardHead,league)).version
    calls=[];predict=service.predict
    def observe(*a,**k):calls.append(1);return predict(*a,**k)
    monkeypatch.setattr(service,'predict',observe)
    updates=[]
    def trace(conn,cursor,statement,*a):
        if statement.startswith('UPDATE published_match_analyses'):updates.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    try:await refresh_one(db,now=NOW+timedelta(minutes=1),force_league=league)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',trace)
    async with db.session() as s:
        row=await s.get(PublishedMatchAnalysis,target)
        assert row.version==version and row.generated_at==generated
        assert (await s.get(BoardHead,league)).version==board_version
    assert updates and all('payload' not in statement for statement in updates)
    assert calls  # A renewed lease must not merely extend a cached old cutoff.


async def test_detail_is_persisted_read_only_and_fails_closed(api,db,monkeypatch):
    import src.api.routes as routes
    import src.analytics.service as service
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,*_=await seed(db)
    async def forbidden(*a,**k):raise AssertionError('HTTP invoked the model')
    monkeypatch.setattr(routes,'compute_statistics',forbidden)
    service._cache.clear()
    writes=[]
    def trace(conn,cursor,statement,*a):
        if statement.split()[0].upper() in ('INSERT','UPDATE','DELETE'):writes.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',trace)
    first=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert first['publication']['status']=='current' and first['candidates']
    for _ in range(3):
        r=await api.get(f'/api/matches/{target}/statistics')
        assert r.status_code==200 and r.json()['publication']==first['publication']
    event.remove(db.engine.sync_engine,'before_cursor_execute',trace)
    assert not writes
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(PublishedMatchAnalysis))==1
        row=await s.get(PublishedMatchAnalysis,target);row.expires_at=NOW-timedelta(seconds=1);await s.commit()
    r=await api.get(f'/api/matches/{target}/statistics')
    assert r.status_code==503 and r.json()['detail']['code']=='analysis_publication_pending'


async def test_history_reads_actual_prematch_archive_without_hindsight_model(api,db,monkeypatch):
    import src.api.routes as routes
    target,*_=await seed(db)
    monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(days=2))
    async def forbidden(*a,**k):raise AssertionError('Retrospective forecast in GET')
    monkeypatch.setattr(routes,'compute_statistics',forbidden)
    async with db.session() as s:
        match=await s.get(Match,target);match.status='finished';match.ft_home=20;match.ft_away=0;await s.commit()
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert data['historical'] and data['publication']['status']=='archived'
    assert data['cache_seconds']==0 and data['match']['ft_home']==20
    assert data['prediction']['expected_home_goals']<20
    assert all(not (r.get('verified_odds') or {}).get('coupon_eligible') for r in data['recommendations'])
    async with db.session() as s:
        row=await s.get(PublishedMatchAnalysis,target);await s.delete(row);await s.commit()
    r=await api.get(f'/api/matches/{target}/statistics')
    assert r.status_code==404 and r.json()['detail']['code']=='analysis_archive_missing'


async def test_detail_revision_refreshes_only_selected_evidence(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    from sqlalchemy.orm import undefer
    from tests.test_minimal_analysis import fixtures,proxy
    from src.analytics.publications import publish_match_analyses
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target=await fixtures(db,4);calls=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':390,'height':900})
        await page.clock.install(time=NOW)
        async def forward(route):
            calls.append(route.request.url)
            await proxy(route,api)
        await page.route('http://test/**',forward)
        try:
            await page.goto('http://test/#predictions');await page.wait_for_load_state('networkidle')
            await expect(page.locator('[data-analysis-block=form]')).to_be_visible()
            await page.locator('[data-analysis-block=form]>summary').click()
            await page.evaluate('window.keptCard=document.querySelector("[data-analysis-match]")')
            count=sum('/api/predictions' in path for path in calls)
            async with db.session() as s:
                row=await s.get(PublishedMatchAnalysis,target,options=[undefer(PublishedMatchAnalysis.payload)])
                payload=deepcopy(row.payload);payload['home_form']['avg_goals_for']+=.01
                await publish_match_analyses(s,{target:payload},row.board_version,row.model_version,NOW,row.expires_at)
                await s.commit();version=row.version
            async with page.expect_response(lambda r:r.url.endswith(f'/api/matches/{target}/statistics')):
                await page.evaluate('BetAppRevision.check()')
            await page.wait_for_load_state('networkidle')
            assert sum('/api/predictions' in path for path in calls)==count
            assert await page.evaluate('keptCard===document.querySelector("[data-analysis-match]")')
            await expect(page.locator('[data-analysis-block=form]')).to_have_attribute('open','')
            assert (await api.get(f'/api/matches/{target}/statistics')).json()['publication']['version']==version
        finally:await page.unroute_all(behavior='ignoreErrors');await browser.close()
