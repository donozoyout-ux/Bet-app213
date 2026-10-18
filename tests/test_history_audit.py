"""Synthetic evidence only. Production audit is read-only and has no apply mode."""
from copy import deepcopy
import json
import os
import uuid

import pytest
from src.analytics.history_audit import evaluate, audit, digest, price_at_capture, stamp, summarize
from src.analytics.history_audit_reader import collect, decode

MATCH = dict(id=1, external_match_id=123, status='finished', kickoff_at='2026-01-02T12:00:00Z',
             ft_home=2, ft_away=1, ht_home=1, ht_away=0)
CAPTURED = '2026-01-02T11:00:00+00:00'


@pytest.mark.parametrize('market,selection,outcome,actual', [
    ('result','home','won','2-1'),('result','away','lost','2-1'),
    ('btts','yes','won','2-1'),('double_chance','x2','lost','2-1'),
    ('goals','over_2_5','won',3),('goals','over_3_0','push',3),
    ('first_half_goals','under_1_5','won',1),('second_half_goals','over_1_5','won',2),
    ('asian_handicap','home_minus_1_0','push','2-1'),
    ('asian_handicap','home_minus_1_25','half_lost','2-1'),
    ('asian_handicap','away_plus_1_25','half_won','2-1'),
    ('asian_totals','over_2_75','half_won',3),
    ('asian_totals','under_2_75','half_lost',3),
])
def test_independent_settlement_edges(market,selection,outcome,actual):
    assert evaluate(MATCH,None,dict(market=market,selection=selection))[:2] == (outcome,actual)


@pytest.mark.parametrize('market,stats', [
    ('cards',dict(is_final=True,home_yellow_cards=2,away_yellow_cards=3,home_red_cards=0)),
    ('corners',dict(is_final=False,home_corners=2,away_corners=3)),
    ('red_cards',dict(is_final=True,home_red_cards=-1,away_red_cards=0)),
])
def test_missing_counts_never_become_losses(market,stats):
    assert evaluate(MATCH,stats,dict(market=market,selection='over_2_5'))[0]=='awaiting_data'


def test_unknown_and_inconsistent_score_abstain():
    assert evaluate(MATCH,None,dict(market='unknown',selection='home'))[0]=='awaiting_data'
    assert evaluate(MATCH,None,dict(market='result',selection=[]))[0]=='awaiting_data'
    assert evaluate(dict(MATCH,ht_home=3),None,dict(market='goals',selection='over_2_5'))[0]=='awaiting_data'
    assert evaluate(dict(MATCH,status='scheduled'),None,dict(market='result',selection='home'))[0]=='pending'


def fixture():
    return dict(match_id=1,league_id=1,league='Test league',captured_at=CAPTURED,model_version='test',
        picks=[dict(market='result',selection='home',probability=.8,confidence='high')],
        match=deepcopy(MATCH),stats=None,decisions=[])


def test_plan_is_idempotent_no_mutations_and_rate_denominators():
    source=fixture();before=deepcopy(source)
    first=audit([source],observed_at='2026-01-03T00:00:00Z')
    assert first==audit([source],observed_at='2026-01-03T00:00:00Z')
    assert source==before
    assert first['discrepancies'][0]['flag']=='missing_terminal_decision'
    assert first['groups']['prediction']['overall']['hit_rate']==1
    assert first['groups']['prediction']['overall']['unit_stake_roi'] is None
    source['captured_at']='2026-01-02T12:00:00Z'
    assert audit([source],observed_at='2026-01-03T00:00:00Z')['groups']['prediction']['overall']['denominator']==0


def test_frozen_evidence_drift_is_not_hash_corruption():
    source=fixture()
    proof=dict(status='finished',ft_home=0,ft_away=1,ht_home=0,ht_away=0,statistics={},is_final=True)
    sh=digest({k:source[k] for k in ('match_id','league_id','captured_at','model_version','picks')})
    decision=dict(id=1,pick_index=0,revision=1,outcome='lost',actual={'value':'0-1'},evidence=proof,
        snapshot_hash=sh,evidence_hash=digest(proof),recorded_at='2026-01-02T15:00:00Z',evaluator_version='settlement-v1')
    source['decisions']=[decision]
    result=audit([source],observed_at='2026-01-03T00:00:00Z')
    assert [r['flag'] for r in result['discrepancies']]==['stored_vs_current_evidence_disagreement']
    source['decisions'][0]['snapshot_hash']='broken'
    result=audit([source],observed_at='2026-01-03T00:00:00Z')
    assert result['groups']['prediction']['overall']['denominator']==0


def test_coupon_capture_pending_is_not_stale_settlement_and_ids_redacted():
    coupon=dict(id='private-coupon-id',created_at=CAPTURED,items=[dict(match={'id':1},
        recommendation=fixture()['picks'][0],status='pending',quote={})])
    result=audit([], [coupon], [dict(MATCH,league='Test',stats=None)], observed_at='2026-01-03T00:00:00Z')
    assert result['discrepancies']==[]
    assert 'private-coupon-id' not in json.dumps(result)


def test_decode_preserves_float_lexemes_for_snapshot_hashes():
    row=decode(dict(picks_json='[{"probability": 1.0}]',decisions='[]',stats=None))
    assert type(row['picks'][0]['probability']) is float
    assert digest(row['picks']) != digest([{'probability':1}])


def test_only_captured_fresh_prices_and_binary_outcomes_enter_metrics():
    pick=dict(market='result',selection='home',verified_odds=dict(market='result',selection='home',period='full_time',has_odds=True,stage='latest',best_bookmaker='Bet365',best_price=2.0,
        observed_at='2026-01-02T10:59:00Z',expires_at='2026-01-02T11:14:00Z'))
    assert price_at_capture(pick,stamp(CAPTURED))==('Bet365',2.0)
    pick['verified_odds']['observed_at']='2026-01-02T11:01:00Z'
    assert price_at_capture(pick,stamp(CAPTURED))[1] is None
    pick['verified_odds']['observed_at']='2026-01-02T10:00:00Z'
    assert price_at_capture(pick,stamp(CAPTURED))[1] is None
    rows=[dict(expected=v,eligible_for_rate=True,probability=.8,price=2.0)
          for v in ['won','lost','push','void','half_won','half_lost','awaiting_data','pending']]
    result=summarize(rows)
    assert result['denominator']==2 and result['hit_rate']==.5
    assert result['priced_n']==5 and result['unit_stake_profit']==0


def test_bookmaker_contract_period_and_home_perspective_line():
    pick=dict(market='asian_handicap',selection='away_plus_0_5')
    quote=dict(price=2,bookmaker='Bet365',line=-.5,period='full_time',
               observed_at='2026-01-02T10:59:00Z',expires_at='2026-01-02T11:14:00Z')
    assert price_at_capture(pick,stamp(CAPTURED),quote)[1]==2
    assert price_at_capture(pick,stamp(CAPTURED),dict(quote,line=.5))[1] is None
    assert price_at_capture(pick,stamp(CAPTURED),dict(quote,period='first_half'))[1] is None


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='requires PostgreSQL service')
async def test_real_postgres_readonly_collection_and_no_writes():
    import asyncpg
    conn=await asyncpg.connect(os.environ['TEST_POSTGRES_URL'].replace('postgresql+asyncpg://','postgresql://'))
    schema='history_audit_'+uuid.uuid4().hex
    try:
        await conn.execute(f'CREATE SCHEMA {schema}')
        await conn.execute(f'SET search_path TO {schema}')
        await conn.execute('''CREATE TABLE leagues(id int PRIMARY KEY,name text);
            CREATE TABLE matches(id int PRIMARY KEY,external_match_id int,league_id int,status text,
                kickoff_at timestamptz,ft_home int,ft_away int,ht_home int,ht_away int,status_observed_at timestamptz);
            CREATE TABLE match_statistics(match_id int,raw json);
            CREATE TABLE prediction_snapshots(match_id int,league_id int,captured_at timestamptz,model_version text,picks json);
            CREATE TABLE prediction_settlements(match_id int,evidence json);
            CREATE TABLE saved_coupons(id text,created_at timestamptz,kind text,revision int,root_id text,payload json);
            INSERT INTO leagues VALUES(1,'Test');
            INSERT INTO matches VALUES(1,123,1,'finished','2026-01-02T12:00Z',2,1,1,0,NULL);
            INSERT INTO prediction_snapshots VALUES(1,1,'2026-01-02T11:00Z','test',
                '[{"market":"result","selection":"home","probability":1.0}]');''')
        with pytest.raises(ValueError,match='read-only'):
            await collect(conn)
        async with conn.transaction(isolation='repeatable_read',readonly=True):
            bundle=await collect(conn,batch_size=1)
            assert len(bundle['snapshots'])==1
            assert type(bundle['snapshots'][0]['picks'][0]['probability']) is float
            result=audit(**bundle)
            assert result['groups']['prediction']['overall']['hit_rate']==1
            # Server, not just Python convention, rejects writes.
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                async with conn.transaction():
                    await conn.execute('DELETE FROM prediction_snapshots')
        assert await conn.fetchval('SELECT count(*) FROM prediction_snapshots')==1
        assert await conn.fetchval('SELECT count(*) FROM prediction_settlements')==0
    finally:
        await conn.execute('SET search_path TO public')
        await conn.execute(f'DROP SCHEMA {schema} CASCADE')
        await conn.close()
