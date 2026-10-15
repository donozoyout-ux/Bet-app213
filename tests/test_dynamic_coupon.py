"""Policy fixtures are synthetic; no fabricated runtime prices or probabilities."""
from copy import deepcopy
from datetime import timedelta
import math
import pytest
from src.analytics.dynamic_coupon import select_portfolio,RISK_BUDGET
from tests.test_predictions import NOW


def entry(id,p=.90,quality=.98,price=2.,league=None):
    return dict(match=dict(id=id,league_id=id if league is None else league,
        home_team_id=id*2,away_team_id=id*2+1,home_team=f'Home {id}',away_team=f'Away {id}',
        kickoff_at=(NOW+timedelta(hours=2)).isoformat()),
        recommendation=dict(id='home_result',market='result',selection='home',qualifies=True,
            label='Home',probability=p,reliability=quality,score=quality,confidence='high'),
        quote=dict(eligible=True,bookmaker='Bet365',price=price,observed_at=NOW.isoformat(),expires_at=(NOW+timedelta(minutes=15)).isoformat()))


def test_no_fixed_size_more_than_four_and_not_every_qualified_match():
    records=[entry(i) for i in range(1,21)]
    selected,audit=select_portfolio(records,NOW)
    assert 4<len(selected)<20
    assert len(selected)==audit['selected_matches']
    assert audit['risk_indicator']<=RISK_BUDGET
    assert audit['qualified_candidates']==20 and len(audit['decisions'])==20
    assert audit['rejections']['combined_risk_budget']>0
    assert audit['evaluations']<=audit['evaluation_budget']
    assert math.prod(i['quote']['price'] for i in selected)>5
    assert [i['match']['id'] for i in select_portfolio(list(reversed(records)),NOW)[0]]==[i['match']['id'] for i in selected]


def test_size_responds_to_quality_and_concentration():
    diverse=select_portfolio([entry(i) for i in range(1,21)],NOW)[0]
    concentrated=select_portfolio([entry(i,league=1) for i in range(1,21)],NOW)[0]
    less_evidence=select_portfolio([entry(i,quality=.80) for i in range(1,21)],NOW)[0]
    assert len(concentrated)<len(diverse)
    assert len(less_evidence)<len(diverse)


def test_marginal_choice_is_not_probability_sorting_and_explains_quality_change():
    records=[entry(1,p=.97,price=1.2),entry(2,p=.90,price=2.),entry(3,p=.94,quality=.80,price=2.)]
    items,audit=select_portfolio(records,NOW)
    first=next(d for d in audit['decisions'] if d.get('selection_order')==1)
    assert first['match_id']==2
    assert first['portfolio_quality_before'] is None
    for item in items:
        d=item['selection_explanation']
        assert d['marginal_utility']>0
        assert d['portfolio_risk_after']==pytest.approx(d['portfolio_risk_before']+d['marginal_risk'])
        assert .8<=d['portfolio_quality_if_added']<=1
    low=next(d for d in audit['decisions'] if d['match_id']==3)
    assert low['quality_dilution_penalty']>0


def test_shared_team_and_same_match_are_not_independent():
    first=entry(1);same=deepcopy(first);same['recommendation']['id']='away_result'
    shared=entry(2);shared['match']['home_team_id']=first['match']['home_team_id']
    selected,audit=select_portfolio([first,same,shared],NOW)
    assert len(selected)==1
    assert audit['rejections']=={'same_match':1,'shared_team':1}


@pytest.mark.parametrize('field,value',[('probability',.699),('reliability',.799),('score',.799),('score',float('nan')),('probability',1.01),('reliability',2),('probability',True),('reliability',True),('score',True)])
def test_quality_is_never_lowered(field,value):
    item=entry(1);item['recommendation'][field]=value
    assert select_portfolio([item],NOW)[0]==[]


def test_stale_price_no_value_and_empty_pool_do_not_force_a_ticket():
    item=entry(1);item['quote']['observed_at']=(NOW-timedelta(seconds=901)).isoformat()
    assert select_portfolio([item],NOW)[1]['rejections']=={'price_rejected':1}
    items,audit=select_portfolio([entry(1,p=.73,price=1.14)],NOW)
    assert items==[] and audit['rejections']=={'non_positive_marginal_utility':1}
    assert select_portfolio([],NOW)[0]==[]


def test_cpu_budget_is_not_a_first_page_or_partial_round_selection(monkeypatch):
    import src.analytics.dynamic_coupon as policy
    monkeypatch.setattr(policy,'MAX_EVALUATIONS',20)
    selected,audit=select_portfolio([entry(i) for i in range(1,21)],NOW)
    assert len(selected)==1 and audit['evaluation_budget_exhausted']
    assert audit['evaluations']==20 and len(audit['decisions'])==20
    monkeypatch.setattr(policy,'MAX_EVALUATIONS',19)
    selected,audit=select_portfolio([entry(i) for i in range(1,21)],NOW)
    assert not selected and audit['evaluations']==0


async def test_api_rejects_user_size_and_preserves_old_records(api,db,monkeypatch):
    from tests.test_automatic_telegram import many_matches
    from src.models import SavedCoupon
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await many_matches(db,12)
    legacy=dict(items=[],combined_odds=1.,target_matches=4,coupon_date='2026-10-07')
    async with db.session() as session:
        session.add(SavedCoupon(id='legacy-automatic',root_id='legacy-automatic',revision=1,kind='automatic',
            owner_hash=None,automatic_fingerprint='legacy-policy-test',created_at=NOW-timedelta(days=1),payload=deepcopy(legacy)))
        await session.commit()
    for value in ('1','4','100','garbage'):
        response=await api.post('/api/coupons/automatic?max_matches='+value)
        assert response.status_code==422
    data=(await api.post('/api/coupons/automatic')).json()
    assert data['selection_mode']=='dynamic' and 'target_matches' not in data
    assert data['selection_policy']['selected_matches']==len(data['items'])
    assert data['selection_policy']['decisions']
    assert all(i['selection_explanation']['marginal_utility']>0 for i in data['items'])
    history=(await api.get('/api/coupons/'+data['id'])).json()
    assert history['selection_mode']=='dynamic'
    previous=(await api.get('/api/coupons/legacy-automatic')).json()
    assert previous['target_matches']==4 and previous['items']==[]
    async with db.session() as session:
        assert (await session.get(SavedCoupon,'legacy-automatic')).payload==legacy


async def test_reused_coupon_has_current_explanations_without_rewriting_history(api,db,monkeypatch):
    from sqlalchemy import select
    from src.models import PredictionBoard,SavedCoupon
    from tests.test_automatic_telegram import many_matches
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await many_matches(db,12)
    first=(await api.post('/api/coupons/automatic')).json()
    selected={i['match']['id'] for i in first['items']}
    async with db.session() as session:
        board=await session.scalar(select(PredictionBoard));data=deepcopy(board.payload)
        excluded=next(i for i in data['items'] if i['match']['id'] not in selected)
        for c in excluded['recommendations']:c['qualifies']=False
        board.payload=data;await session.commit()
    second=(await api.post('/api/coupons/automatic')).json()
    assert second['id']==first['id']
    assert second['selection_policy']['qualified_candidates']==first['selection_policy']['qualified_candidates']-2
    assert second['selection_policy']==second['diagnostics']['selection']
    async with db.session() as session:
        saved=await session.get(SavedCoupon,first['id'])
        assert saved.payload['selection_policy']==first['selection_policy']
