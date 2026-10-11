"""Stored synthetic fixtures exercise real odds, API validation and browser drafts."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import os
import uuid

import pytest
from sqlalchemy import select,func
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema,DropSchema
from src.models import Match, MarketOdds, Bookmaker, PredictionBoard, PredictionSnapshot, MatchStatistics
from src.analytics.odds_matching import match_odds_for_candidate
from src.analytics.coupon import DraftRequest,quote_draft,settle_draft
from src.analytics.board_cache import MODEL_VERSION
from src.analytics.performance import settle
from src.jobs.storage import merge_odds_observations
from src.db import Database
from tests.test_predictions import seed,NOW
from tests.test_recommendations import candidate
from src.analytics.recommendations import rank_candidate


async def coupon_seed(db):
    id,league,season,teams,_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,id)
        second=Match(external_match_id=898989,league_id=league,season_id=season,round=2,
            home_team_id=teams[1],away_team_id=teams[0],kickoff_at=NOW+timedelta(days=1),status='scheduled',raw={},created_at=NOW,updated_at=NOW)
        session.add(second);await session.flush()
        board=await session.get(PredictionBoard,league)
        template=deepcopy(board.payload['items'][0]);items=[]
        for fixture in (match,second):
            item=deepcopy(template);item['match']['id']=fixture.id;item['match_id']=fixture.id
            picks=[]
            for market,selection,label in [('first_half_goals','over_1_5','1.5 İlk Yarı Gol Üst'),('corners','under_9_5','9.5 Korner Alt')]:
                rec=rank_candidate(candidate(market,market,.70,selection));rec['label']=label;picks.append(rec)
            item['recommendations']=picks;item['strongest_prediction']=picks[0]['id'];items.append(item)
            for book in await session.scalars(select(Bookmaker)):
                for market,period,line,price in [('first_half_goals','first_half',1.5,1.8),('corners','full_time',9.5,2.2)]:
                    session.add(MarketOdds(match_id=fixture.id,bookmaker_id=book.id,market=market,period=period,latest_line=line,latest_over=price,latest_under=price,updated_at=NOW-timedelta(minutes=1),raw={}))
        board.payload={**board.payload,'items':items,'evaluated':2}
        board.expires_at=NOW+timedelta(minutes=10);board.model_version=MODEL_VERSION
        await session.commit()
        return id,second.id


def request(ids,markets=('first_half_goals','corners')):
    selections=[]
    for id,market in zip(ids,markets):
        selection='over_1_5' if market=='first_half_goals' else 'under_9_5'
        selections.append(dict(match_id=id,recommendation_id=selection+'_'+market,bookmaker='Bet365'))
    return {'selections':selections}


async def verify_quote_and_settlement(db):
    ids=await coupon_seed(db)
    async with db.session() as session:
        snapshots=await session.scalar(select(func.count(PredictionSnapshot.match_id)))
        data=await quote_draft(session,DraftRequest(**request(ids)),NOW)
        assert data['eligible'] and data['combined_odds']==pytest.approx(3.96)
        assert data['items'][0]['period']=='first_half'
        assert data['items'][1]['line']==9.5
        first=await session.get(Match,ids[0]);first.status='finished';first.ht_home=1;first.ht_away=1
        second=await session.get(Match,ids[1]);second.status='finished'
        session.add(MatchStatistics(match_id=ids[1],is_final=True,home_corners=6,away_corners=5,raw={}))
        await session.commit()
        settled=await settle_draft(session,DraftRequest(**request(ids)))
        assert [i['status'] for i in settled['items']]==['won','lost']
        assert await session.scalar(select(func.count(PredictionSnapshot.match_id)))==snapshots
        assert not (await quote_draft(session,DraftRequest(**request(ids)),NOW))['eligible']


async def test_sqlite_coupon_real_tables(db):
    await verify_quote_and_settlement(db)


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_coupon_real_tables():
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='coupon_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await verify_quote_and_settlement(db)
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


async def test_coupon_api_revalidates_unchanged_cached_board(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids=await coupon_seed(db)
    assert (await api.post('/api/coupon/quote',json=request(ids))).json()['combined_odds']==3.96
    assert (await api.post('/api/coupon/quote',json=request([ids[0],ids[0]]))).status_code==422
    invalid=request(ids);invalid['selections'].append(invalid['selections'][0])
    assert (await api.post('/api/coupon/quote',json=invalid)).status_code==422
    invalid=request(ids);invalid['selections'][0]['bookmaker']='Unknown'
    assert (await api.post('/api/coupon/quote',json=invalid)).status_code==422
    page=(await api.get('/api/predictions/best')).json()
    assert len(page['items'])==4
    assert all(i['recommendation']['verified_odds']['has_odds'] for i in page['items'])
    async with db.session() as session:
        row=await session.scalar(select(MarketOdds).where(MarketOdds.match_id==ids[0],MarketOdds.market=='first_half_goals',MarketOdds.bookmaker_id==select(Bookmaker.id).where(Bookmaker.name=='Bet365').scalar_subquery()))
        row.latest_line=2.5;await session.commit()
    # No board rebuild: the web request still detects a moved line.
    quote=(await api.post('/api/coupon/quote',json=request(ids))).json()
    assert not quote['eligible'] and quote['combined_odds'] is None
    assert quote['items'][0]['reason']=='no_verified_odds'
    page=(await api.get('/api/predictions/best')).json()
    pick=next(i['recommendation'] for i in page['items'] if i['match']['id']==ids[0] and i['recommendation']['market']=='first_half_goals')
    assert 'Bet365' not in pick['verified_odds']['bookmakers']
    async with db.session() as session:
        for row in await session.scalars(select(MarketOdds)):row.updated_at=NOW-timedelta(hours=1)
        await session.commit()
    assert not (await api.post('/api/coupon/quote',json=request(ids))).json()['eligible']


def test_fresh_price_priority_and_best_price_timestamp():
    def row(price,stamp,period='first_half'):
        return SimpleNamespace(match_id=1,market='first_half_goals',period=period,latest_line=1.5,latest_over=price,updated_at=stamp,raw={})
    candidate=dict(market='first_half_goals',selection='over_1_5',probability=.7)
    rows=[(row(5,NOW-timedelta(hours=1)),'Crown'),(row(1.8,NOW-timedelta(minutes=3)),'Bet365'),(row(1.7,NOW-timedelta(minutes=1)),'Sbobet')]
    vo=match_odds_for_candidate(candidate,1,'scheduled',period_odds_rows=rows,cutoff=NOW)
    assert vo['best_price']==1.8 and vo['observed_at']==(NOW-timedelta(minutes=3)).isoformat()
    assert vo['bookmakers']['Crown']['freshness']=='stale'
    assert not vo['bookmakers']['Crown']['coupon_eligible']
    assert vo['bookmakers']['Bet365']['coupon_eligible']
    assert not match_odds_for_candidate(candidate,1,'scheduled',period_odds_rows=[(row(2,NOW,None),'Bet365')],cutoff=NOW)['has_odds']
    assert not match_odds_for_candidate(candidate,1,'finished',period_odds_rows=rows,cutoff=NOW)['has_odds']


def test_partial_scrape_keeps_old_price_time_and_line(monkeypatch):
    import src.jobs.storage as storage
    monkeypatch.setattr(storage,'utcnow',lambda:NOW)
    row=MarketOdds(match_id=1,bookmaker_id=1,market='corners',period='full_time',latest_line=9.5,latest_over=1.9,latest_under=1.9,updated_at=NOW-timedelta(hours=1),raw={})
    merge_odds_observations(row,{'latest_line':10.5,'latest_under':2.0,'raw':{}})
    # Retained old price is neither assigned the new line nor made fresh.
    assert row.latest_over==1.9
    over=dict(market='corners',selection='over_10_5',probability=.7)
    vo=match_odds_for_candidate(over,1,'scheduled',period_odds_rows=[(row,'Bet365')],cutoff=NOW)
    assert not vo['has_odds']
    under=dict(market='corners',selection='under_10_5',probability=.7)
    assert match_odds_for_candidate(under,1,'scheduled',period_odds_rows=[(row,'Bet365')],cutoff=NOW)['coupon_eligible']


@pytest.mark.parametrize('status,ht,expected',[('scheduled',(None,None),'pending'),('live',(1,1),'pending'),('finished',(1,1),'won'),('finished',(1,0),'lost'),('finished',(None,None),'awaiting_data')])
def test_first_half_draft_outcomes(status,ht,expected):
    match=SimpleNamespace(status=status,ht_home=ht[0],ht_away=ht[1],ft_home=None,ft_away=None)
    assert settle(match,None,dict(market='first_half_goals',selection='over_1_5'))[0]==expected


async def test_chromium_coupon_add_copy_clear_and_settlement(api,db,monkeypatch):
    from playwright.async_api import async_playwright,expect
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    ids=await coupon_seed(db)
    async with async_playwright() as p:
        browser=await p.chromium.launch();page=await browser.new_page(viewport={'width':1280,'height':900})
        await page.clock.install(time=NOW)
        await page.add_init_script("window.copiedText='';Object.defineProperty(navigator,'clipboard',{value:{writeText:async(text)=>{window.copiedText=text;}}});")
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def forward(route):
            response=await api.request(route.request.method,route.request.url.removeprefix('http://test'),content=route.request.post_data,headers={'content-type':'application/json'})
            await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
        await page.route('http://test/**',forward);await page.goto('http://test/#coupon')
        await page.locator('#coupon-edit-panel summary').click()
        checks=page.locator('[data-coupon-pick]');await expect(checks).to_have_count(4)
        await checks.nth(0).check();await expect(page.locator('#coupon-selected')).to_contain_text('İlk Yarı')
        await expect(checks.nth(1)).to_be_disabled()
        await checks.nth(3).check();await expect(page.locator('#coupon-total')).to_contain_text('3,96')
        await page.locator('#coupon-copy').click();await expect(page.locator('#coupon-note')).to_contain_text('kopyalandı')
        assert 'Bet365' in await page.evaluate('window.copiedText')
        async with db.session() as session:
            match=await session.get(Match,ids[0]);match.status='finished';match.ht_home=1;match.ht_away=1;await session.commit()
        await page.locator('#coupon-reload').click();await expect(page.locator('#coupon-selected')).to_contain_text('Kazandı')
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.locator('#coupon-clear').click();await expect(page.locator('#coupon-selected')).to_contain_text('Henüz seçim yapılmadı')
        assert errors==[];await browser.close()
