from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import re
import pytest
from sqlalchemy import select,func
from src.scrapers.goaloo.statistics import parse_statistics,STAT_FIELDS
from src.scrapers.goaloo.client import SourceError
from src.models import Match,MatchStatistics,MatchEvent,League,ScraperJob,JobItem,Referee
from src.jobs.storage import store_statistics
from src.analytics.match_statistics import summary,count_prediction,poisson_over
from tests.test_api import seed as seed_real_schedule
from tests.test_predictions import seed,NOW


def captured(league=36):
    text=Path(f'tests/fixtures/match_statistics_{league}.html').read_text(encoding='utf-8')
    mid=int(re.search(r"sId: parseInt\('(\d+)'",text)[1])
    return parse_statistics(text,mid,league)


@pytest.mark.parametrize('league',[36,31,34,8,30,75])
def test_live_captured_structures_and_no_guessed_referee(league):
    data=captured(league)
    assert data['is_final'] and data['collection_status']=='available'
    assert data['statistics']['home_corners'] is not None
    assert data['statistics']['home_corners_ht'] is not None
    assert data['statistics']['home_shots_on_target'] is not None
    assert data['events'] and data['referee'] is None


def test_epl_fields_and_absent_red_is_null():
    data=captured();s=data['statistics']
    assert (s['home_corners'],s['away_corners'],s['home_corners_ht'],s['away_corners_ht'])==(7,8,2,1)
    assert (s['home_yellow_cards'],s['away_yellow_cards'])==(2,3)
    assert s['home_red_cards'] is s['away_red_cards'] is None
    assert (s['home_possession'],s['away_possession'])==(55,45)
    assert captured(31)['statistics']['home_corners_ht']==0
    assert captured(30)['statistics']['home_yellow_cards']==0
    assert captured(8)['statistics']['away_yellow_cards']==0
    assert captured(75)['statistics']['away_red_cards']==2


def test_missing_identity_and_missing_statistics_never_become_zero():
    with pytest.raises(SourceError):parse_statistics('<html>Page Not Found</html>',1)
    text="<script>var _matchInfo={sId:parseInt('1'),sclassId:parseInt('36'),state:parseInt('-1')}</script>"
    data=parse_statistics(text,1,36)
    assert all(value is None for value in data['statistics'].values())
    assert not data['events_available'] and data['events']==[]
    # An empty scaffold is unavailable, not evidence that previously captured
    # incidents have all been deleted from the match.
    empty=parse_statistics(text+'<table id="eventsTable"><tr><th>Events</th></tr></table>',1,36)
    assert not empty['events_available']
    with pytest.raises(SourceError):parse_statistics(text,2,36)


def test_event_minutes_players_substitution_and_secondary_name():
    events=captured()['events']
    goal=next(e for e in events if e['event_type']=='goal')
    assert (goal['minute'],goal['player_name'],goal['secondary_player_name'])==(87,'Zirkzee J.','Garnacho A.')
    sub=next(e for e in events if e['minute']==90 and e['stoppage_minute']==1)
    assert sub['event_type']=='substitution' and sub['team_side']=='away'
    assert sub['player_name'] and sub['secondary_player_name']
    assert len({e['source_key'] for e in events})==len(events)


def metric_rows(league=75,n=30):
    s=captured(league)['statistics'];rows=[]
    for i in range(n):
        match=SimpleNamespace(id=i,home_team_id=1 if i%2 else 2,away_team_id=2 if i%2 else 1,status='finished')
        rows.append((match,SimpleNamespace(**s,is_final=True)))
    return rows


def test_corner_and_card_averages_denominators_and_null_red():
    rows=metric_rows(75,5)
    corners=summary(rows,1,'corners')
    assert corners['total_avg']==4 and corners['sample_size']==5
    assert corners['over_rates']['over_7_5']==0
    cards=summary(rows,1,'cards')
    assert cards['total_avg']==6 and cards['over_rates']['over_5_5']==1
    epl=summary(metric_rows(36,5),1,'cards')
    assert epl['yellow_cards_for_avg'] is not None and epl['for_avg'] is None and epl['total_avg'] is None
    assert epl['red_cards_for_avg'] is None and epl['sample_size']==0
    assert all(v is None for v in epl['over_rates'].values())


def test_corner_card_poisson_and_minimum_samples():
    rows=metric_rows()
    corners=count_prediction(rows,1,2,'corners')
    cards=count_prediction(rows,1,2,'cards')
    assert corners['status']==cards['status']=='ok'
    assert corners['expected_total']==pytest.approx(4)
    assert cards['expected_total']==pytest.approx(6)
    assert cards['basis']=='yellow_plus_red'
    assert cards['over_probabilities']['3.5']>=cards['over_probabilities']['4.5']>=cards['over_probabilities']['5.5']
    assert all(0<=p<=1 for p in corners['over_probabilities'].values())
    assert count_prediction(rows[:4],1,2,'corners')['status']=='insufficient_data'
    assert count_prediction(metric_rows(36),1,2,'cards')['expected_total'] is None
    assert poisson_over(0,8.5)==0


async def test_persist_idempotent_snapshot_zero_null_and_events(db):
    match_id=await seed_real_schedule(db)
    data=captured()
    async with db.session() as session:
        match=await session.get(Match,match_id)
        await store_statistics(session,match,data)
        await session.commit()
        await store_statistics(session,match,data)
        await session.commit()
        stored=await session.get(MatchStatistics,match_id)
        assert stored.home_corners==7 and stored.home_red_cards is None
        assert await session.scalar(select(func.count(MatchStatistics.match_id)))==1
        assert await session.scalar(select(func.count(MatchEvent.id)))==len(data['events'])
        assert match.referee_id is None
        partial=deepcopy(data);partial['events_available']=False;partial['events']=[]
        await store_statistics(session,match,partial);await session.commit()
        assert await session.scalar(select(func.count(MatchEvent.id)))==len(data['events'])


async def test_additional_statistics_api_splits_and_no_future_leakage(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target_id,_,_,_,history=await seed(db)
    data=captured(75)
    async with db.session() as session:
        for id in history:
            match=await session.get(Match,id)
            row=MatchStatistics(match_id=id,**data['statistics'],is_final=True,updated_at=NOW-timedelta(days=1),raw={})
            session.add(row)
        await session.commit()
    first=(await api.get(f'/api/matches/{target_id}/statistics')).json()
    assert first['prediction']['corners_status']==first['prediction']['cards_status']=='ok'
    extra=first['additional_statistics']
    assert extra['corners']['home']['last_5']['matches_considered']==5
    assert extra['cards']['home']['home_split']['sample_size']==40
    assert extra['cards']['away']['away_split']['sample_size']==40
    async with db.session() as session:
        row=await session.get(MatchStatistics,history[0]);row.home_corners=999;row.updated_at=NOW+timedelta(days=1)
        await session.commit()
    second=(await api.get(f'/api/matches/{target_id}/statistics')).json()
    assert second['additional_statistics']['corners']['home']['last_5']['matches_considered']==5
    assert second['additional_statistics']['corners']['home']['last_5']['sample_size']==4
    assert second['prediction']['expected_total_corners']<30


async def test_stats_backfill_failure_and_resume_skips_completed(db,monkeypatch):
    import src.jobs.worker as worker
    await seed_real_schedule(db)
    monkeypatch.setattr(worker,'database',db)
    calls=[];failed=True
    async def fetch(client,id,league):
        calls.append(id)
        if failed and id==2590898:raise SourceError('Temporary source failure')
        data=deepcopy(captured());data['raw']['match_id']=id
        return data
    monkeypatch.setattr(worker,'fetch_statistics',fetch)
    job=await worker.enqueue('stats_backfill')
    await worker.work(once=True)
    async with db.session() as session:
        j=await session.get(ScraperJob,job.id);assert j.status=='partial' and j.failed_matches==1 and j.processed_matches==9
    calls.clear();failed=False
    await worker.enqueue('stats_backfill',resume_id=job.id)
    await worker.work(once=True)
    assert calls==[2590898]
    async with db.session() as session:
        assert (await session.get(ScraperJob,job.id)).status=='completed'
        assert await session.scalar(select(func.count(MatchStatistics.match_id)))==10
    # A new discovery does not repeatedly scrape already observed unavailable/partial rows.
    job=await worker.enqueue('stats_backfill');await worker.work(once=True)
    async with db.session() as session:assert (await session.get(ScraperJob,job.id)).total_matches==0


@pytest.mark.parametrize('n',[9,10])
async def test_referee_minimums_from_stored_history_only(api,db,monkeypatch,n):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    target,_,_,_,history=await seed(db)
    async with db.session() as session:
        # Synthetic identity only for unit-testing aggregation; the live parser
        # has no verified identity and emits None for all six captured sources.
        referee=Referee(source_key='unit-test-referee',name='Unit test referee',source='test')
        session.add(referee);await session.flush()
        for id in [target,*history[:n]]:
            match=await session.get(Match,id);match.referee_id=referee.id;match.referee_observed_at=NOW-timedelta(days=1);match.updated_at=NOW-timedelta(hours=1)
            if id!=target:session.add(MatchStatistics(match_id=id,**captured(75)['statistics'],is_final=True,updated_at=NOW-timedelta(days=1),raw={}))
        await session.commit()
    result=(await api.get(f'/api/matches/{target}/statistics')).json()['additional_statistics']['referee']
    assert result['matches_officiated']==n
    assert result['average_total_yellow_cards']==(3 if n==10 else None)
    assert result['average_total_red_cards']==(3 if n==10 else None)
    assert result['average_total_fouls']==(23 if n==10 else None)
    assert result['status']==('ok' if n==10 else 'insufficient_data')
