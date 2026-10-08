"""Synthetic unit-test inputs only; runtime predictions use stored observations."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import pytest
from sqlalchemy import select
from src.models import League, Season, Team, Match, Odds1X2, Bookmaker
from src.analytics.predictions import Result, team_stats, implied_probabilities, poisson_probabilities, predict
from src.analytics.service import statistics, _cache

NOW=datetime(2026,10,8,12,tzinfo=timezone.utc)


@pytest.mark.parametrize('home,away',[(0,0),(.2,.1),(1.74,1.12),(8,8),(2,0)])
def test_poisson_probabilities_normalized_and_stable(home,away):
    result=poisson_probabilities(home,away)
    assert sum(result[key] for key in ['home_probability','draw_probability','away_probability'])==pytest.approx(1,abs=1e-12)
    assert result['over_25_probability']+result['under_25_probability']==pytest.approx(1)
    assert result['btts_probability']+result['no_btts_probability']==pytest.approx(1)
    assert all(math.isfinite(value) and 0<=value<=1 for value in result.values())
    assert result==poisson_probabilities(home,away)


def test_bookmaker_margin_removed_and_missing_prices_rejected():
    result=implied_probabilities({'home':2,'draw':3,'away':4})
    assert sum(result[key] for key in ['home','draw','away'])==pytest.approx(1)
    assert result['home']==pytest.approx((1/2)/(1/2+1/3+1/4))
    assert result['overround']==pytest.approx(1/2+1/3+1/4-1)
    for price in [None,0,1,-2,float('inf'),float('nan')]:
        assert implied_probabilities({'home':price,'draw':3,'away':4}) is None


def test_form_goal_rates_btts_and_clean_sheets():
    rows=[Result(i,1,2,h,a,NOW-timedelta(days=i)) for i,(h,a) in enumerate([(2,0),(1,1),(0,2),(3,2),(1,0)])]
    stats=team_stats(rows,1)
    assert stats['form']==['G','B','M','G','G']
    assert (stats['wins'],stats['draws'],stats['losses'])==(3,1,1)
    assert stats['goals_for']==7 and stats['goals_against']==5
    assert stats['avg_goals_for']==1.4 and stats['avg_goals_against']==1
    assert stats['over_25_rate']==.2 and stats['btts_rate']==.4
    assert stats['clean_sheet_rate']==.4
    assert team_stats(rows,2)['form']==['M','B','G','M','M']
    assert team_stats([],1)['avg_goals_for'] is None
    assert team_stats(rows+[Result(999,3,4,99,0,NOW)],1)==stats


def test_insufficient_history_has_no_numeric_prediction():
    prediction,_=predict([],1,2,'club')
    assert prediction['status']=='insufficient_data'
    assert 'home_probability' not in prediction and 'expected_home_goals' not in prediction


def test_zero_baseline_abstains_without_overconfident_probabilities():
    rows=[Result(i,1 if i%2 else 2,2 if i%2 else 1,0,0,NOW-timedelta(days=i+2)) for i in range(30)]
    prediction,_=predict(rows,1,2,'club')
    assert prediction['status']=='insufficient_data'
    assert 'draw_probability' not in prediction


async def seed(db):
    async with db.session() as session:
        league=await session.scalar(select(League).where(League.external_id==36))
        season=Season(league_id=league.id,season_name='2026-2027');session.add(season)
        teams=[Team(external_id=900000+i,name=f'Test team {i}') for i in range(4)];session.add_all(teams)
        await session.flush()
        ids=[team.id for team in teams]
        history=[]
        # Both focal teams have real test rows in both venues; every observation precedes NOW.
        for i in range(80):
            home,away=([ids[0],ids[1]] if i%2==0 else [ids[1],ids[0]])
            when=NOW-timedelta(days=i+2)
            row=Match(external_match_id=990000+i,league_id=league.id,season_id=season.id,round=1,
                      kickoff_at=when,home_team_id=home,away_team_id=away,status='finished',ft_home=2,ft_away=1,
                      created_at=when,updated_at=when+timedelta(hours=3),last_scraped_at=when+timedelta(hours=3),raw={})
            session.add(row);history.append(row)
        target=Match(external_match_id=999999,league_id=league.id,season_id=season.id,round=2,
                     kickoff_at=NOW+timedelta(days=1),home_team_id=ids[0],away_team_id=ids[1],status='scheduled',raw={},created_at=NOW,updated_at=NOW)
        session.add(target);await session.flush()
        for book in (await session.scalars(select(Bookmaker))).all():
            session.add(Odds1X2(match_id=target.id,bookmaker_id=book.id,opening_home=2,opening_draw=3,opening_away=4,
                               latest_home=2,latest_draw=3,latest_away=4,raw={'r':{'u':999}},updated_at=NOW-timedelta(minutes=1)))
        await session.commit()
        return target.id, league.id, season.id, ids, [row.id for row in history]


async def test_statistics_api_form_splits_h2h_and_market(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,_,_=await seed(db)
    response=await api.get(f'/api/matches/{match_id}/statistics')
    assert response.status_code==200,response.text
    data=response.json();p=data['prediction']
    assert p['status']=='ok'
    assert p['home_probability']+p['draw_probability']+p['away_probability']==pytest.approx(1)
    assert data['home_form']['sample_size']==5 and data['home_last_10']['sample_size']==10
    assert all(row['home']==data['match']['home_team'] for row in data['home_split']['matches'])
    assert all(row['away']==data['match']['away_team'] for row in data['away_split']['matches'])
    assert data['h2h_summary']['sufficient'] and len(data['h2h'])==5
    assert data['bookmaker_consensus']['bookmaker_count']==3
    assert all(book['stage']=='latest' and book['available_as_of'] for book in data['bookmakers'])
    assert sum(data['bookmaker_consensus'][key] for key in ['home','draw','away'])==pytest.approx(1)
    assert (await api.get('/api/matches/9999999/statistics')).status_code==404


async def test_future_and_late_observed_results_do_not_leak(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,league,season,ids,_=await seed(db)
    before=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    async with db.session() as session:
        for index,(kickoff,observed) in enumerate([(NOW+timedelta(days=2),NOW-timedelta(days=1)),(NOW-timedelta(days=10),NOW+timedelta(days=1)),(NOW-timedelta(hours=1),NOW-timedelta(minutes=5))]):
            session.add(Match(external_match_id=888000+index,league_id=league,season_id=season,round=1,kickoff_at=kickoff,
                              home_team_id=ids[0],away_team_id=ids[1],status='finished',ft_home=50,ft_away=0,raw={},created_at=observed,updated_at=observed,last_scraped_at=observed))
        await session.commit()
    after=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert before['prediction']==after['prediction']
    assert before['home_form']==after['home_form']


async def test_historical_observation_cutoff_and_late_market_exclusion(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,_,history=await seed(db)
    async with db.session() as session:
        target=await session.get(Match,match_id)
        target.status='finished';target.kickoff_at=NOW-timedelta(days=1);target.ft_home=8;target.ft_away=0
        # This prior result was only observed AFTER the historical target kickoff.
        row=await session.get(Match,history[0]);row.updated_at=NOW;row.ft_home=99
        await session.commit()
    result=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert result['historical']
    assert result['bookmaker_consensus'] is None
    assert not any(book['available_as_of'] for book in result['bookmakers'])
    assert all(row['home_goals']!=99 for row in result['home_form']['matches'])
    assert result['prediction']['league_sample_size']==79


@pytest.mark.parametrize('kind',['club','national'])
async def test_competition_and_national_history_isolation(api,db,monkeypatch,kind):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,ids,_=await seed(db)
    before=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    async with db.session() as session:
        league=League(external_id=87654,name='Test isolated competition',country='Test',competition_type=kind)
        session.add(league);await session.flush()
        season=Season(league_id=league.id,season_name='2026');session.add(season);await session.flush()
        session.add(Match(external_match_id=876540,league_id=league.id,season_id=season.id,round=1,kickoff_at=NOW-timedelta(days=3),
                          home_team_id=ids[0],away_team_id=ids[1],status='finished',ft_home=100,ft_away=0,raw={},created_at=NOW-timedelta(days=3),updated_at=NOW-timedelta(days=3)))
        await session.commit()
    after=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert before['prediction']==after['prediction']


async def test_predictions_nearest_window_league_date_and_insufficient(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,league,season,ids,_=await seed(db)
    async with db.session() as session:
        for index,hours in enumerate([2,48,144,24*120]):
            session.add(Match(external_match_id=777000+index,league_id=league,season_id=season,round=2,kickoff_at=NOW+timedelta(hours=hours),
                              home_team_id=ids[2],away_team_id=ids[3],status='scheduled',raw={},created_at=NOW,updated_at=NOW))
        await session.commit()
    page=(await api.get('/api/predictions?limit=8')).json()
    assert page['window_hours']==168 and len(page['items'])==4
    dates=[row['match']['kickoff_at'] for row in page['items']]
    assert dates==sorted(dates)
    assert page['items'][0]['prediction']['status']=='insufficient_data'
    assert page['items'][0]['prediction']['home_probability'] is None
    assert (await api.get('/api/predictions?league=999999')).json()['items']==[]
    assert len((await api.get('/api/predictions?date=2026-10-09')).json()['items'])==1
    assert (await api.get('/api/predictions?limit=100')).status_code==422


async def test_cache_invalidates_after_score_update(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,_,history=await seed(db)
    first=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    async with db.session() as session:
        row=await session.get(Match,history[0]);row.ft_home=4;row.updated_at=NOW-timedelta(minutes=1);await session.commit()
    second=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert first['prediction']['expected_home_goals']!=second['prediction']['expected_home_goals']


async def test_cache_reuses_calculation_without_mutation(api,db,monkeypatch):
    import src.api.routes as routes
    import src.analytics.service as service
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,_,_=await seed(db)
    original=service.predict;calls=[]
    def count(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(service,'predict',count)
    await api.get(f'/api/matches/{match_id}/statistics')
    await api.get(f'/api/matches/{match_id}/statistics')
    assert len(calls)==1


def test_national_confidence_cannot_be_high():
    history=[Result(i,1,2,2,1,NOW-timedelta(days=i+1)) if i%2==0 else Result(i,2,1,2,1,NOW-timedelta(days=i+1)) for i in range(120)]
    prediction,_=predict(history,1,2,'national',{'home':.6,'draw':.2,'away':.2,'bookmaker_count':3})
    assert prediction['confidence']!='high'
    assert prediction['confidence_rules']['high_sample_rules']


async def test_national_prediction_and_h2h_not_required(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    match_id,_,_,ids,history=await seed(db)
    async with db.session() as session:
        league=League(external_id=87655,name='Test national competition',country='Test',competition_type='national')
        session.add(league);await session.flush()
        season=Season(league_id=league.id,season_name='2026');session.add(season);await session.flush()
        patterns=[(ids[0],ids[2]),(ids[2],ids[0]),(ids[1],ids[3]),(ids[3],ids[1])]
        for index,match in enumerate((await session.scalars(select(Match).where(Match.id.in_(history)))).all()):
            match.home_team_id,match.away_team_id=patterns[index%4]
            match.league_id,match.season_id=league.id,season.id
            match.updated_at=NOW-timedelta(minutes=10)
        target=await session.get(Match,match_id);target.league_id,target.season_id=league.id,season.id
        await session.commit()
    data=(await api.get(f'/api/matches/{match_id}/statistics')).json()
    assert data['match']['competition_type']=='national'
    assert data['prediction']['status']=='ok'
    assert data['prediction']['confidence']!='high'
    assert data['h2h']==[] and not data['h2h_summary']['sufficient']
