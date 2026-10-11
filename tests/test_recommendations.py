from copy import deepcopy
import pytest
from src.analytics.recommendations import rank_candidate,select_recommendations,market_matches,build_candidates
from src.analytics.predictions import handicap_probability,poisson_probabilities
from tests.test_predictions import seed,NOW


def candidate(market='result',group='outcome',probability=.68,selection='home',strong=True):
    return {'id':selection+'_'+market,'market':market,'selection':selection,'label':selection,'probability':probability,
            'correlation_group':group,'bookmaker_count':3 if strong else 0,'market_probability':probability-.02 if strong else None,
            'evidence':{'home_sample':10 if strong else 5,'away_sample':10 if strong else 5,'home_venue':10 if strong else 3,
                'away_venue':10 if strong else 3,'league_sample':120 if strong else 20,'sample_size':30 if strong else 10,
                'stability':1 if strong else .4,'season_agreement':1 if strong else .2,'completeness':1 if strong else .5,
                'confidence':'high' if strong else 'low','competition_type':'club'}}


@pytest.mark.parametrize('n',[0,1,2,3])
def test_zero_to_three_by_threshold_not_forced(n):
    candidates=[candidate('result','outcome'),candidate('goals','goal_environment',.70,'over_2_5'),candidate('corners','corners',.66,'over_9_5')][:n]
    candidates.append(candidate('cards','cards',.95,'over_4_5',strong=False))
    ranked,selected=select_recommendations(candidates)
    assert len(selected)==min(n,2)
    assert not any(r['market']=='cards' for r in selected)
    assert all(r['qualifies'] for r in selected)


def test_correlation_groups_suppress_nested_and_result_handicap_duplicates():
    candidates=[candidate('goals','goal_environment',p,selection) for p,selection in [(.82,'over_1_5'),(.72,'over_2_5'),(.64,'over_3_5')]]
    candidates+=[candidate('result','outcome'),candidate('asian_handicap','outcome',.75,'home_minus_0_5'),candidate('double_chance','outcome',.90,'1x')]
    _,selected=select_recommendations(candidates)
    assert len(selected)==2
    assert len({r['correlation_group'] for r in selected})==2


def test_strong_sample_66_outranks_weak_75_and_conflict_is_penalized():
    strong=candidate(probability=.66);weak=candidate(probability=.75,strong=False)
    assert rank_candidate(strong)['score']>rank_candidate(weak)['score']
    assert not rank_candidate(weak)['qualifies']
    conflicting=deepcopy(strong);conflicting['evidence'].update(stability=.4,season_agreement=.1)
    assert rank_candidate(conflicting)['score']<rank_candidate(strong)['score']
    assert not rank_candidate(conflicting)['qualifies']


def test_filters_read_selected_not_raw_candidates_and_different_market_combinations():
    _,a=select_recommendations([candidate('result'),candidate('cards','cards',.75,'over_4_5',strong=False)])
    _,b=select_recommendations([candidate('cards','cards',.72,'over_4_5'),candidate('corners','corners',.66,'over_9_5'),candidate('btts','goal_environment',.61,'yes')])
    assert not market_matches(a,'cards')
    assert len(b) == 2
    assert market_matches(b,'cards') and market_matches(b,'corners')
    assert not market_matches(b,'goals')
    assert {r['market'] for r in a}=={'result'}
    assert {r['market'] for r in b}=={'cards','corners'}


def test_supported_half_handicap_probability_and_no_push_guess():
    p=poisson_probabilities(1.6,1.1)
    assert handicap_probability(1.6,1.1,-.5,'home')==pytest.approx(p['home_probability'])
    assert handicap_probability(1.6,1.1,.5,'away')==pytest.approx(p['draw_probability']+p['away_probability'])
    assert handicap_probability(1.6,1.1,-.25,'home') is None
    assert handicap_probability(1.6,1.1,-1,'home') is None


async def test_api_selected_filter_global_rank_and_full_candidates(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    id,_,_,_,_=await seed(db)
    stats=(await api.get(f'/api/matches/{id}/statistics')).json()
    assert len(stats['candidates'])>=14
    assert all(any(r['id']==c['id'] for c in stats['candidates']) for r in stats['recommendations'])
    page=(await api.get('/api/predictions?limit=8')).json()
    assert len(page['items'])==1
    item=page['items'][0]
    assert item['strongest_prediction']==item['recommendations'][0]['id']
    assert 1<=len(item['recommendations'])<=3
    assert (await api.get('/api/predictions?market=cards')).json()['items']==[]
    global_page=(await api.get('/api/predictions/best')).json()
    scores=[row['recommendation']['score'] for row in global_page['items']]
    assert scores==sorted(scores,reverse=True)
    assert len(global_page['items'])==len(item['recommendations'])
    assert global_page['evaluated_matches']==1


async def test_presentation_sort_and_pick_pagination(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    await seed(db)
    rows=(await api.get('/api/matches?view=upcoming&sort=team')).json()['items']
    assert [r['home_team'] for r in rows]==sorted(r['home_team'] for r in rows)
    second=(await api.get('/api/matches?view=upcoming&sort=team&limit=1&offset=1')).json()['items']
    assert second==rows[1:2]
    picks=(await api.get('/api/predictions/best')).json()['items']
    assert (await api.get('/api/predictions/best?limit=1&offset=1')).json()['items']==picks[1:2]
    assert (await api.get('/api/matches?sort=unsupported')).status_code==422
    assert (await api.get('/api/predictions/best?offset=-1')).status_code==422


def test_card_recommendations_only_supported_lines():
    prediction={'status':'insufficient_data'}
    values={}
    for market in ('cards','yellow_cards'):
        form={'sample_size':10,'matches_considered':10,'total_avg':3.2}
        values[market]={'prediction':{'status':'ok','home_sample_size':10,'away_sample_size':10,'home_split_sample_size':8,'away_split_sample_size':8,'league_sample_size':100,'sample_size':20,'basis':'yellow_only','over_probabilities':{str(x):.4 for x in (2.5,3.5,4.5,5.5,6.5)}},'home':{'last_5':form,'last_10':form,'season':form},'away':{'last_5':form,'last_10':form,'season':form}}
    result={'prediction':prediction,'additional_statistics':values,'match':{'competition_type':'club'}}
    candidates=build_candidates(result)
    card_candidates=[c for c in candidates if c['market'] in ('cards','yellow_cards')]
    assert len(card_candidates)==12
    assert all(c['selection'].endswith(('_2_5','_3_5','_4_5')) for c in card_candidates)
    assert all(not c['selection'].endswith(('_5_5','_6_5')) for c in card_candidates)
