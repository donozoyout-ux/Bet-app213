from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select,event
from src.models import Match,PredictionSnapshot,PredictionSettlement
from src.scrapers.goaloo.live import parse_scoreboard
from src.jobs.storage import store_live
from src.jobs.settlement import sweep
from src.analytics.settlements import settle_match,snapshot_digest
from tests.test_live_updater import seed_match,NOW


def payload(half="<span title='Score 1st Half'>1-0</span>",*,state=-1):
    return {'ErrCode':0,'Data':{'state':state,'html':"<div class='end'><div class='score'>2</div><div>Finished ("+half+",<span title='Score 2nd Half'>1-1</span>)</div><div class='score'>1</div></div>"}}


@pytest.mark.parametrize('half,expected',[
    ("<span title='Score 1st Half'>1-0</span>",(1,0)),
    ("<span title='Score 1st Half'>0-0</span>",(0,0)),
    ("<span title='Score 1st Half'>3-0</span>",(None,None)),
    ("<span title='Score 1st Half'>1-2</span>",(None,None)),
    ("<span title='Score 1st Half'>-</span>",(None,None)),
    ("<span title='Score 2nd Half'>1-0</span>",(None,None)),
    ("<span class title>1-0</span>",(None,None)),
    ("<span title='Score 1st Half'>1-0</span><span title='Score 1st Half'>0-0</span>",(None,None)),
    ('',(None,None)),
])
def test_explicit_scoped_halftime_only(half,expected):
    data=payload(half)
    data['Data']['html']+="<span title='Score 1st Half'>9-9</span>"
    result=parse_scoreboard(data)
    assert (result['ht_home'],result['ht_away'])==expected
    assert (result['ft_home'],result['ft_away'])==(2,1)


def test_first_half_in_progress_is_not_a_completed_halftime_score():
    assert parse_scoreboard(payload(state=1))['ht_home'] is None


async def saved_match(db,*,known=None,raw_score=None):
    league,_=await seed_match(db)
    async with db.session() as s:
        m=await s.get(Match,5959);m.status='finished';m.ft_home=2;m.ft_away=1
        m.status_observed_at=NOW-timedelta(minutes=1)
        if known is not None:m.ht_home=known
        observation=parse_scoreboard(payload())
        m.raw={'live':observation['raw']}
        if raw_score:m.raw['live']['score_html']=raw_score
        snap=PredictionSnapshot(match_id=m.id,league_id=league,captured_at=m.kickoff_at-timedelta(hours=1),model_version='fixture',
            picks=[dict(id='goals',market='goals',selection='over_2_5',probability=.8),
                   dict(id='half',market='first_half_goals',selection='over_0_5',probability=.8),
                   dict(id='second',market='second_half_goals',selection='over_1_5',probability=.8)])
        s.add(snap);await s.flush();await settle_match(s,m);await s.commit()
        return snapshot_digest(snap),deepcopy(m.raw)


async def test_worker_recovers_stored_evidence_then_settles_idempotently(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'utcnow',lambda:NOW)
    digest,raw=await saved_match(db)
    # HTTP reads cannot repair source fields or insert terminal records.
    await api.get('/api/performance-trends')
    async with db.session() as s:
        assert (await s.get(Match,5959)).ht_home is None
        original=(await s.scalars(select(PredictionSettlement))).one()
        original_id,original_hash=original.id,original.evidence_hash
    sql=[]
    def capture(conn,cursor,statement,parameters,context,executemany):sql.append(statement)
    event.listen(db.engine.sync_engine,'before_cursor_execute',capture)
    try: assert await sweep(db,NOW)
    finally:event.remove(db.engine.sync_engine,'before_cursor_execute',capture)
    assert any('matches.raw' in statement and 'LIMIT' in statement for statement in sql)
    async with db.session() as s:
        m=await s.get(Match,5959)
        assert (m.ht_home,m.ht_away)==(1,0)
        assert m.raw['live']==raw['live'] and 'halftime_recovery' in m.raw
        assert snapshot_digest(await s.get(PredictionSnapshot,5959))==digest
        records=list(await s.scalars(select(PredictionSettlement).order_by(PredictionSettlement.id)))
        assert len(records)==3 and all(x.outcome=='won' for x in records)
        assert (records[0].id,records[0].evidence_hash)==(original_id,original_hash)
        ids=[r.id for r in records]
    assert await sweep(db,NOW+timedelta(seconds=61))
    async with db.session() as s:
        assert list(await s.scalars(select(PredictionSettlement.id).order_by(PredictionSettlement.id)))==ids


@pytest.mark.parametrize('known,raw_score',[(0,None),(None,"<div class='end'><div class='score'>3</div><span title='Score 1st Half'>1-0</span><div class='score'>1</div></div>")])
async def test_recovery_never_overwrites_conflicting_or_different_score(db,known,raw_score):
    await saved_match(db,known=known,raw_score=raw_score)
    await sweep(db,NOW)
    async with db.session() as s:
        m=await s.get(Match,5959)
        assert (m.ht_home,m.ht_away)==(known,None)
        assert len(list(await s.scalars(select(PredictionSettlement))))==1


async def test_new_live_observation_persists_half_before_settlement(db):
    await saved_match(db)
    async with db.session() as s:
        m=await s.get(Match,5959)
        await store_live(s,m,parse_scoreboard(payload()),NOW)
        await s.commit()
        assert (m.ht_home,m.ht_away)==(1,0)
        assert len(list(await s.scalars(select(PredictionSettlement))))==3
