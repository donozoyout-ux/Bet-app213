"""Independent card markets: synthetic histories, real persistence/API/browser paths."""
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.analytics.match_statistics import summary, count_prediction
from src.analytics.performance import capture, settle
from src.analytics.recommendations import build_candidates, select_recommendations
from src.analytics.service import statistics
from src.db import Database
from src.models import Match, MatchStatistics, PredictionSnapshot
from tests.test_predictions import seed, NOW
from tests.test_match_statistics import metric_rows


def test_independent_verified_samples_and_unknown_reds():
    rows=metric_rows(36,80)  # captured yellows 2/3, no reds
    yellow=summary(rows,1,'cards','yellow_only')
    red=summary(rows,1,'cards','red_only')
    combined=summary(rows,1,'cards')
    assert yellow['sample_size']==80 and yellow['total_avg']==5
    assert red['sample_size']==combined['sample_size']==0
    assert red['total_avg'] is combined['total_avg'] is None
    assert combined['missing_total_sample_size']==80
    assert count_prediction(rows,1,2,'cards','yellow_only')['status']=='ok'
    assert count_prediction(rows,1,2,'cards','red_only')['status']=='insufficient_data'
    rows[0][1].home_red_cards=0
    one=summary(rows[:1],1,'cards','red_only')
    assert one['against_sample_size']==1 and one['against_avg']==0
    assert one['sample_size']==0 and one['for_avg'] is None


def test_red_model_does_not_require_yellow_and_zero_baseline_abstains():
    rows=metric_rows(75,80)
    for _,s in rows:
        s.home_yellow_cards=s.away_yellow_cards=None
        s.home_red_cards=s.away_red_cards=1
    assert count_prediction(rows,1,2,'cards','red_only')['expected_total']==2
    assert count_prediction(rows,1,2,'cards','yellow_only')['expected_total'] is None
    assert count_prediction(rows,1,2,'cards')['expected_total'] is None
    for _,s in rows:s.home_red_cards=s.away_red_cards=0
    assert summary(rows,1,'cards','red_only')['total_avg']==0
    assert count_prediction(rows,1,2,'cards','red_only')['insufficient_reasons']==[{'code':'zero_league_baseline'}]


def test_settlement_preserves_published_combined_definition():
    match=SimpleNamespace(status='finished')
    stats=SimpleNamespace(is_final=True,home_yellow_cards=2,away_yellow_cards=3,home_red_cards=None,away_red_cards=None)
    pick={'selection':'over_4_5','market':'cards'}
    assert settle(match,stats,pick)==('awaiting_data',None)
    assert settle(match,stats,{**pick,'market':'yellow_cards'})==('won',5)
    assert settle(match,stats,{**pick,'market':'red_cards'})==('awaiting_data',None)
    stats.home_red_cards=stats.away_red_cards=0
    assert settle(match,stats,pick)==('won',5)
    assert settle(match,stats,{'selection':'under_0_5','market':'red_cards'})==('won',0)


@pytest.mark.parametrize('basis,market',[('yellow_only','yellow_cards'),('red_only','red_cards')])
def test_independent_markets_qualify_only_with_strong_evidence(basis,market):
    rows=metric_rows(75,80)
    for _,s in rows:
        s.home_yellow_cards=s.away_yellow_cards=1
        s.home_red_cards=s.away_red_cards=1
    def candidates(history):
        profile={key:summary(history[:n],1,'cards',basis) for key,n in [('last_5',5),('last_10',10),('season',80)]}
        result={'prediction':{'status':'insufficient_data'},'match':{'competition_type':'club'},
            'additional_statistics':{market:{'home':profile,'away':profile,'prediction':count_prediction(history,1,2,'cards',basis)}}}
        return build_candidates(result)
    ranked,selected=select_recommendations(candidates(rows))
    assert len(selected)==1 and selected[0]['market']==market
    assert all(c['correlation_group']=='cards' for c in ranked)
    ranked,selected=select_recommendations(candidates(rows[:30]))
    assert ranked and not selected
    assert all('league_samples' in c['rejection_reasons'] for c in ranked)


async def seed_cards(db,n=80,refresh=True):
    target,league,season,teams,history=await seed(db)
    async with db.session() as session:
        for id in history[:n]:
            session.add(MatchStatistics(match_id=id,is_final=True,home_yellow_cards=1,away_yellow_cards=1,
                home_red_cards=None,away_red_cards=None,updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit()
    if refresh:
        from src.analytics.board_cache import refresh_one
        await refresh_one(db, now=NOW, force_league=league)
    return target,history



async def verify_persistent_analytics(db):
    target,history=await seed_cards(db,refresh=False)
    async with db.session() as session:

        from src.api.routes import match_query, match_response
        query,_,_=match_query()
        row=(await session.execute(query.where(Match.id==target))).first()
        result=await statistics(session,row[0],match_response(row),NOW)
        extra=result['additional_statistics']
        assert extra['yellow_cards']['prediction']['status']=='ok'
        assert extra['cards']['prediction']['status']==extra['red_cards']['prediction']['status']=='insufficient_data'
        selected=result['recommendations']
        assert 0<len(selected)<=3
        assert any(r['market']=='yellow_cards' and 'Sarı Kart' in r['label'] for r in selected)
        assert all(r['qualifies'] for r in selected)
        assert sum(r['correlation_group']=='cards' for r in selected)==1
        for side in ('home','away'):
            for window in ('last_5','last_10','home_split','away_split'):
                assert extra['yellow_cards'][side][window]['for_avg']==1
                assert extra['red_cards'][side][window]['for_avg'] is None
        # A pre-existing published combined pick is immutable even after adding yellow models.
        match=await session.get(Match,target)
        old=[{'market':'cards','selection':'over_4_5','label':'4.5 Kart Üst'}]
        existing=await session.get(PredictionSnapshot,target)
        if existing:
            existing.picks=old;existing.model_version='quality-poisson-v1'
        else:
            session.add(PredictionSnapshot(match_id=target,league_id=match.league_id,captured_at=NOW,
                model_version='quality-poisson-v1',picks=old))
        await session.commit()
        assert not await capture(session,match,selected,NOW)
        saved=await session.get(PredictionSnapshot,target)
        assert saved.picks==old and saved.model_version=='quality-poisson-v1'

        stored=await session.get(MatchStatistics,history[0])
        assert stored.home_red_cards is stored.away_red_cards is None


async def test_sqlite_card_analytics_and_immutable_history(db):
    await verify_persistent_analytics(db)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_card_analytics_and_immutable_history():
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='cards_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        await verify_persistent_analytics(db)
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


async def test_api_yellow_market_filters_and_descriptive_insufficient_history(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,_=await seed_cards(db)
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert data['prediction']['yellow_cards_status']=='ok'
    assert data['prediction']['cards_status']==data['prediction']['red_cards_status']=='insufficient_data'
    assert data['prediction']['card_basis']=='yellow_plus_red'
    for path in ('/api/predictions','/api/predictions/best'):
        response=await api.get(path+'?market=yellow_cards')
        assert response.status_code==200 and response.json()['items']
        combined=(await api.get(path+'?market=cards')).json()
        assert not combined['items']
        assert combined['availability']['markets']['cards']['model_ready_matches']==0
    for market in ('yellow_cards','red_cards','cards'):
        assert (await api.get('/api/market-performance?market='+market)).status_code==200


async def test_browser_card_subtabs_descriptions_without_qualified_card_picks(api,db,monkeypatch):
    from pathlib import Path
    from playwright.async_api import async_playwright, expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,_=await seed_cards(db,3)
    data=(await api.get(f'/api/matches/{target}/statistics')).json()
    assert not any(r['correlation_group']=='cards' for r in data['recommendations'])
    assert data['additional_statistics']['yellow_cards']['home']['last_5']['for_avg']==1
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        page=await browser.new_page(viewport={'width':1280,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            response=await api.get(route.request.url.removeprefix('http://test'))
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward)
        await page.goto('http://test/')
        await page.evaluate('(id)=>BetAppDashboard.selectMatch(id,true)',target)
        await page.locator('[data-analysis-tab=cards]').click()
        panel=page.locator('#card-analysis-content')
        await expect(panel).to_contain_text('1.00 / 1.00')
        await expect(panel).to_contain_text('Son 5')
        await expect(panel).to_contain_text('Son 10')
        await expect(panel).to_contain_text('Ev sahibi tarafı')
        await expect(panel).to_contain_text('Deplasman tarafı')
        await expect(panel).to_contain_text('seçilmiş güçlü öneri yok')
        async def screenshot(name):
            if os.getenv('CARD_SCREENSHOT_DIR'):
                folder=Path(os.environ['CARD_SCREENSHOT_DIR']);folder.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(folder/name),full_page=False,animations='disabled')
        await screenshot('card-yellow-desktop.png')
        await page.locator('#card-tab-red_cards').click()
        await expect(panel).to_contain_text('Bilinmeyen kırmızı kartlar sıfır değildir')
        await expect(panel).to_contain_text('— / —')
        await screenshot('card-red-desktop.png')
        await page.keyboard.press('ArrowRight')
        await expect(page.locator('#card-tab-cards')).to_be_focused()
        await expect(panel).to_contain_text('Sarı ve kırmızı birlikte gerekli')
        await screenshot('card-combined-desktop.png')
        await page.set_viewport_size({'width':390,'height':844})
        await page.locator('#card-tab-yellow_cards').click()
        await expect(panel).to_contain_text('1.00 / 1.00')
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert await page.locator('#analysis-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
        await screenshot('card-yellow-mobile.png')
        assert errors==[]
        await browser.close()
