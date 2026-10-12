from datetime import timedelta
from copy import deepcopy
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from src.models import MarketOdds,CollectorRetry,Match,PredictionBoard
from src.models import SavedCoupon,TelegramDelivery
from src.jobs.live import PriorityUpdater
from src.analytics.odds_matching import match_odds_for_candidate
from src.analytics.performance import tally,add,finish
from src.analytics.recommendations import select_recommendations
from tests.test_recommendations import candidate
from tests.test_coupon import coupon_seed,NOW


async def test_upcoming_quotes_refresh_markets_and_durable_backoff(db,monkeypatch):
    import src.jobs.live as live
    import src.jobs.storage as storage
    import src.scrapers.goaloo.odds as odds
    import src.scrapers.goaloo.corner_odds as corners
    ids=await coupon_seed(db);calls=[]
    monkeypatch.setattr(live,'utcnow',lambda:NOW)
    monkeypatch.setattr(storage,'utcnow',lambda:NOW)
    async def ht(*args):
        calls.append('ht');return {'Bet365':{'ou':dict(latest_line=1.5,latest_over=1.9,latest_under=1.8,raw={})}}
    async def corner(*args,**kwargs):
        calls.append('corners');return {'Bet365':dict(latest_line=9.5,latest_over=2.1,latest_under=2.3,raw={})}
    monkeypatch.setattr(odds,'fetch_halftime_odds',ht);monkeypatch.setattr(corners,'fetch_corner_odds',corner)
    updater=PriorityUpdater(db)
    for _ in range(5):await updater.upcoming_quotes(None)
    assert calls.count('ht')==2 and calls.count('corners')==2
    async with db.session() as session:
        rows=(await session.scalars(select(MarketOdds).where(MarketOdds.match_id.in_(ids),MarketOdds.market=='corners'))).all()
        assert any(row.latest_under==2.3 and row.raw['_field_observed_at']['latest_under']==NOW.isoformat() for row in rows)
        assert len((await session.scalars(select(CollectorRetry).where(CollectorRetry.key.like('quotes:%')))).all())==4
    # A failed endpoint is backed off; another market still proceeds.
    monkeypatch.setattr(live,'utcnow',lambda:NOW+timedelta(minutes=6))
    async def failure(*args):raise TimeoutError()
    monkeypatch.setattr(odds,'fetch_halftime_odds',failure)
    for _ in range(4):await updater.upcoming_quotes(None)
    async with db.session() as session:
        retries=(await session.scalars(select(CollectorRetry))).all()
        assert sum(r.reason=='TimeoutError' for r in retries)==2
        assert sum(r.reason=='quote_refresh' for r in retries)==2


def test_odds_diagnostic_distinguishes_exact_line_stale_and_no_source():
    row=SimpleNamespace(match_id=1,market='corners',period='full_time',latest_line=9.5,latest_over=1.8,latest_under=1.9,updated_at=NOW-timedelta(hours=1),raw={'_field_observed_at':{'latest_over':(NOW-timedelta(hours=1)).isoformat()}})
    def match(market,selection):return match_odds_for_candidate(dict(market=market,selection=selection,probability=.7),1,'scheduled',period_odds_rows=[(row,'Bet365')],cutoff=NOW)
    assert match('corners','over_9_5')['reason']=='odds_stale'
    assert match('corners','over_8_5')['reason']=='line_mismatch'
    assert match('first_half_goals','over_1_5')['reason']=='no_source_rows'
    assert match('second_half_goals','over_1_5')['reason']=='source_market_unavailable'


def test_calibration_roi_uses_only_saved_fresh_prematch_prices():
    result=tally()
    pick=dict(probability=.7,reliability=.85,score=.85,sample_size=30,verified_odds=dict(has_odds=True,stage='latest',best_price=2.,observed_at=(NOW-timedelta(minutes=1)).isoformat()))
    add(result,pick,'won',NOW);add(result,pick,'lost',NOW)
    future=deepcopy(pick);future['verified_odds']['observed_at']=(NOW+timedelta(seconds=1)).isoformat()
    add(result,future,'won',NOW)
    stale=deepcopy(pick);stale['verified_odds']['observed_at']=(NOW-timedelta(hours=1)).isoformat()
    add(result,stale,'lost',NOW)
    data=finish(result)
    assert data['settled']==4 and data['success_rate']==.5
    assert data['brier_score']==pytest.approx(.29)
    assert data['priced_samples']==2 and data['roi']==0
    assert data['calibration'][0]['samples']==4


async def test_coverage_and_empty_coupon_explain_rejections(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await coupon_seed(db)
    coverage=(await api.get('/api/odds-coverage')).json()
    assert coverage['markets']['corners']['fresh_prices']==2
    assert coverage['markets']['first_half_goals']['matched_prices']==2
    empty=(await api.post('/api/coupons/automatic')).json()
    assert empty['items']==[] and empty['status']=='no_eligible_selections'
    assert 'message' in empty and 'diagnostics' in empty
    assert (await api.post('/api/coupons/automatic?max_matches=5')).status_code==422


def test_exact_priced_alternative_is_selected_without_loosening_quality():
    unpriced=candidate('corners','corners',.85,'under_9_5')
    priced=candidate('corners','corners',.75,'under_8_5')
    priced['verified_odds']={'coupon_eligible':True}
    weak=candidate('result','outcome',.95,'home',strong=False)
    weak['verified_odds']={'coupon_eligible':True}
    _,selected=select_recommendations([unpriced,priced,weak])
    assert [p['id'] for p in selected]==['under_8_5_corners']
    priced['verified_odds']['coupon_eligible']=False
    _,selected=select_recommendations([unpriced,priced,weak])
    assert [p['id'] for p in selected]==['under_9_5_corners']


async def test_uncertain_noon_is_not_resent_with_new_evening_observations(db,monkeypatch):
    import httpx
    from src.jobs.telegram import dispatch
    from tests.test_automatic_telegram import coupon_seed as daily_seed,freshen,TOKEN,CHAT
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN',TOKEN);monkeypatch.setenv('TELEGRAM_CHAT_ID',CHAT)
    await daily_seed(db);noon=NOW.replace(hour=9);await freshen(db,noon)
    calls=[]
    def timeout(req):calls.append(req);raise httpx.ReadTimeout('timeout',request=req)
    transport=httpx.MockTransport(timeout)
    assert (await dispatch(db,noon,transport=transport))['status']=='uncertain'
    evening=NOW.replace(hour=15);await freshen(db,evening)
    assert (await dispatch(db,evening,transport=transport))['status']=='duplicate_selections'
    assert len(calls)==1
    async with db.session() as session:
        assert len((await session.scalars(select(TelegramDelivery))).all())==1
        assert len((await session.scalars(select(SavedCoupon))).all())==2


async def test_rescheduled_database_date_cannot_be_hidden_by_cached_today(db):
    from tests.test_automatic_telegram import coupon_seed as daily_seed
    from src.analytics.saved_coupons import automatic
    from src.analytics.coupon import quote_draft,DraftRequest
    from tests.test_coupon import request
    ids=await daily_seed(db)
    async with db.session() as session:
        assert await automatic(session,NOW) is not None
        for id in ids:(await session.get(Match,id)).kickoff_at=NOW+timedelta(days=8)
        await session.commit()
        assert await automatic(session,NOW) is None
        data=await quote_draft(session,DraftRequest(**request(ids)),NOW)
        assert not data['eligible']
        assert all(i['reason']=='outside_manual_window' for i in data['items'])
