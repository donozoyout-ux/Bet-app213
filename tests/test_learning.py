from copy import deepcopy
from datetime import timedelta
import math
import os
import time
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from src.analytics.learning_metrics import metrics, analyze, normalize, METHOD, losses
from src.analytics.learning_calibration import temporal_split, evaluate_calibration, paired_bounds
from src.analytics.learning import refresh, source_data, publish_head
from src.analytics.settlements import settle_match, decisions, correct_settlement
from src.models import Match, LearningReport, LearningLesson, LearningSlice, LearningHead, PredictionSnapshot
from tests.test_predictions import seed, NOW


def row(index, p=.8, y=1, match_id=None, day=None):
    kickoff=NOW+timedelta(days=index if day is None else day)
    return dict(match_id=index if match_id is None else match_id,pick_index=0,
        league_id=1,market='goals',model_version='test-v1',probability=p,y=y,
        kickoff_at=kickoff,available_at=kickoff+timedelta(hours=2),revision=1,settlement_id=index)


def item(r):
    return dict(match_id=r['match_id'],pick_index=r['pick_index'],league_id=r['league_id'],
        model_version=r['model_version'],prediction=dict(market=r['market'],probability=r['probability']),
        outcome='won' if r['y'] else 'lost',kickoff_at=r['kickoff_at'],recorded_at=r['available_at'],
        revision=r['revision'],settlement_id=r['settlement_id'])


def test_scores_calibration_and_probability_boundaries():
    result=metrics([row(1,.8,1),row(2,.6,0)])
    assert result['success_rate']==.5
    assert result['mean_probability']==pytest.approx(.7)
    assert result['observed_minus_expected']==pytest.approx(-.2)
    assert result['brier_score']==pytest.approx(.2)
    assert result['log_loss']==pytest.approx(-(math.log(.8)+math.log(.4))/2)
    assert sum(b['predictions'] for b in result['calibration'])==2
    assert metrics([])['success_rate'] is None
    for p in (0,1):
        assert all(math.isfinite(x) for x in losses(p,1))
    assert metrics([row(1,1,1)])['calibration'][0]['upper_inclusive']


def test_dependent_picks_do_not_add_independent_evidence():
    repeated=[row(i,.8,1,match_id=1) for i in range(300)]
    independent=[row(i,.8,1) for i in range(300)]
    assert metrics(repeated)['effective_matches']==1
    assert metrics(independent)['effective_matches']==300
    assert metrics(repeated)['success_interval'][0] < metrics(independent)['success_interval'][0]
    report=analyze([item(r) for r in repeated])
    assert all(l['payload']['status']=='insufficient_sample' for l in report['lessons'])
    assert all(not l['payload']['automatic_model_change'] for l in report['lessons'])


@pytest.mark.parametrize('bad', [None,True,-.1,1.1,float('nan'),float('inf'),'0.8'])
def test_void_pending_invalid_probabilities_excluded(bad):
    good=item(row(1));invalid=item(row(2));invalid['prediction']['probability']=bad
    void=dict(item(row(3)),outcome='void');pending=dict(item(row(4)),outcome='pending')
    rows,excluded=normalize([good,invalid,void,pending])
    assert len(rows)==1 and excluded==dict(invalid_probability=1,void=1,pending=1)


def test_lessons_scopes_and_reproducibility():
    items=[item(row(i,.99,0)) for i in range(1200)]
    result=analyze(items)
    assert result==analyze(items)
    assert {s['kind'] for s in result['slices']}=={'league','market','model','league_market','calibration'}
    supported=[l['payload'] for l in result['lessons'] if l['payload']['status']=='supported_candidate']
    assert supported and all(l['evidence']['family_tests']==5 for l in supported)
    assert all(l['confidence']=='moderate' for l in supported)


def test_temporal_split_uses_whole_dates_and_asof_outcomes():
    rows=[row(i,day=i//2) for i in range(1200)]
    train,validation,test,boundaries=temporal_split(rows)
    assert max(r['kickoff_at'] for r in train) < min(r['kickoff_at'] for r in validation)
    assert max(r['kickoff_at'] for r in validation) < min(r['kickoff_at'] for r in test)
    assert not ({r['match_id'] for r in train}&{r['match_id'] for r in test})
    late=deepcopy(rows)
    late[0]['available_at']=NOW+timedelta(days=1000)
    again,_,_,info=temporal_split(late)
    assert late[0] not in again and info['excluded_late_outcomes']==1


def test_calibration_never_fits_small_sample_or_late_backfill():
    assert not evaluate_calibration([row(i) for i in range(121)])['fitted']
    correlated=[row(i,match_id=1) for i in range(3000)]
    assert not evaluate_calibration(correlated)['fitted']
    rows=[row(i,p=.7,y=i%2) for i in range(1000)]
    for r in rows:r['available_at']=NOW+timedelta(days=5000)
    result=evaluate_calibration(rows)
    assert not result['fitted'] and result['reason']=='insufficient_time_eligible_matches'


def test_candidate_fit_rejects_unreliable_improvement_and_keeps_live_input():
    rows=[row(i,p=.7,y=i%2) for i in range(600)]
    before=deepcopy(rows)
    result=evaluate_calibration(rows)
    assert result['fitted'] and result['status']=='rejected'
    assert result['reason']=='heldout_improvement_not_reliable'
    assert not result['automatic_model_change'] and rows==before
    assert result['test_candidate']['brier_score'] < result['test_baseline']['brier_score']
    assert result==evaluate_calibration(rows)


def test_paired_gate_requires_reliable_improvement_on_both_scores():
    original=[row(i,p=.999,y=0) for i in range(2000)]
    calibrated=[dict(r,probability=.001) for r in original]
    bounds=paired_bounds(original,calibrated)
    assert bounds['brier_delta_upper'] < 0 and bounds['log_loss_delta_upper'] < 0
    small=paired_bounds(original[:10],calibrated[:10])
    assert small['log_loss_delta_upper'] > 0


def test_strong_heldout_improvement_is_only_an_offline_candidate():
    result=evaluate_calibration([row(i,p=.999,y=int(i%10==0)) for i in range(600)])
    assert result['status']=='offline_candidate' and result['fitted']
    assert result['comparison']['log_loss_delta_upper'] < 0
    assert result['comparison']['brier_delta_upper'] < 0
    assert not result['automatic_model_change']
    mixed=[row(i) for i in range(600)];mixed[0]['model_version']='another-version'
    assert evaluate_calibration(mixed)['reason']=='mixed_model_versions'


async def finished(db):
    target,*_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,target);match.status='finished';match.ft_home=8;match.ft_away=7
        await settle_match(session,match);await session.commit()
    return target


async def test_refresh_revisions_immutable_editions_and_idempotency(db,api):
    target=await finished(db)
    first=await refresh(db)
    assert first==await refresh(db)
    async with db.session() as session:
        original=await session.get(LearningReport,first)
        old_summary=deepcopy(original.summary)
        match=await session.get(Match,target);match.ft_home=0;match.ft_away=0
        from src.analytics.performance import settle
        snapshot=await session.get(PredictionSnapshot,target)
        frozen=await decisions(session,target)
        index=next(i for i,r in frozen.items() if settle(match,None,snapshot.picks[i])[0]!=r.outcome)
        await correct_settlement(session,target,index,expected_revision=1,actor='test',reason='confirmed score correction')
        await session.commit()
    second=await refresh(db)
    assert first!=second and second==await refresh(db)
    async with db.session() as session:
        assert (await session.get(LearningReport,first)).summary==old_summary
        assert (await session.get(LearningReport,second)).summary!=old_summary
        assert await session.scalar(select(func.count(LearningReport.id)))==2
        assert await session.scalar(select(func.count(LearningLesson.id)))>0
        for table in ('learning_reports','learning_slices','learning_lessons'):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():await session.execute(text(f'DELETE FROM {table}'))
    assert (await api.get('/api/learning/summary')).json()['report_id']==second
    assert (await api.get('/api/learning/summary',params={'report_id':first})).json()['summary']==old_summary
    assert (await api.get('/api/learning/lessons?limit=1&offset=1')).json()['report_id']==second


async def test_api_reads_precomputed_only_and_validates_pagination(db,api,monkeypatch):
    assert (await api.get('/api/learning/summary')).json()['status']=='warming'
    target=await finished(db);report=await refresh(db)
    import src.analytics.learning_metrics as engine
    def forbidden(*args,**kwargs):raise AssertionError('Request attempted analysis')
    monkeypatch.setattr(engine,'analyze',forbidden)
    for endpoint in ('summary','coverage','reports','statistics','lessons'):
        response=await api.get('/api/learning/'+endpoint)
        assert response.status_code==200
    data=(await api.get('/api/learning/coverage')).json()['coverage']
    assert data['eligible_predictions']>0 and data['match_pending']==0
    assert (await api.get('/api/learning/lessons?limit=101')).status_code==422
    assert (await api.get('/api/learning/statistics?kind=arbitrary')).status_code==422
    assert (await api.get('/api/learning/statistics?offset=-1')).status_code==422
    assert (await api.get('/api/learning/summary?report_id=999999')).status_code==404
    assert (await api.post('/api/learning/summary')).status_code==405
    page=(await api.get('/api/learning/lessons?limit=1')).json()
    assert len(page['items'])==1 and page['total']>1
    assert page['items'][0]['status']=='insufficient_sample'
    from sqlalchemy import inspect
    async with db.session() as session:
        stored=await session.get(LearningReport,report)
        assert 'source_manifest' in inspect(stored).unloaded


async def test_head_revisits_old_fingerprint_and_blocks_stale_publish(db):
    await finished(db);first=await refresh(db)
    async with db.session() as session:
        initial=await session.get(LearningHead,METHOD);stamp=initial.source_observed_at
        copy=LearningReport(method_version=METHOD,data_fingerprint='f'*64,source_observed_at=stamp,
            summary={},coverage={},source_manifest=[])
        session.add(copy);await session.flush()
        await publish_head(session,copy.id,NOW+timedelta(days=5000));await session.commit()
        await publish_head(session,first,NOW);await session.commit()
        await session.refresh(initial)
        assert initial.report_id==copy.id
        await publish_head(session,first,NOW+timedelta(days=5001));await session.commit()
        await session.refresh(initial)
        assert initial.report_id==first


def test_large_input_linear_grouping_cost():
    # 20k picks, 10k match clusters: enough to catch per-pick quadratic scans.
    rows=[item(row(i,p=.7,y=i%2,match_id=i//2,day=i//2)) for i in range(20000)]
    start=time.monotonic();result=analyze(rows);elapsed=time.monotonic()-start
    assert result['summary']['predictions']==20000 and result['summary']['matches']==10000
    assert elapsed < 20
    print(f'learning metrics: 20000 picks / 10000 matches in {elapsed:.3f}s')


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_learning_atomic_concurrent_publication_and_recalculation():
    import asyncio,uuid
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='learning_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await db.initialize()
        await finished(db)
        ids=await asyncio.gather(*(refresh(db) for _ in range(4)))
        assert len(set(ids))==1
        async with db.session() as session:
            assert await session.scalar(select(func.count(LearningReport.id)))==1
            assert await session.scalar(select(func.count(LearningLesson.id)))>0
            for table in ('learning_reports','learning_slices','learning_lessons'):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():await session.execute(text(f'UPDATE {schema}.{table} SET id=id'))
            items,coverage,_,_=await source_data_new_session(db)
            assert items and coverage['recorded_terminal']==len(items)
        async with db.session() as session:
            match=await session.get(Match,items[0]['match_id']);match.ft_home=0;match.ft_away=0
            frozen=await decisions(session,match.id);snapshot=await session.get(PredictionSnapshot,match.id)
            from src.analytics.performance import settle
            index=next(i for i,r in frozen.items() if settle(match,None,snapshot.picks[i])[0]!=r.outcome)
            await correct_settlement(session,match.id,index,expected_revision=1,actor='reviewer',reason='verified correction')
            await session.commit()
        newer=await refresh(db)
        assert newer!=ids[0] and newer==await refresh(db)
        async with db.session() as session:
            assert await session.scalar(select(func.count(LearningReport.id)))==2
        # Bulk-published, hash-valid historical fixtures exercise actual paged
        # PostgreSQL ingestion, not just arithmetic over an in-memory list.
        from sqlalchemy import insert,event
        from src.models import PredictionSettlement
        from src.analytics.settlements import snapshot_digest,digest,VERSION
        from types import SimpleNamespace
        async with db.session() as session:
            base=await session.get(Match,items[0]['match_id'])
            matches=[];snapshots=[];results=[]
            picks=[dict(market='goals',selection='over_2_5',probability=.7)]
            for i in range(2000):
                identity=100000+i;kickoff=NOW-timedelta(days=i+2)
                matches.append(dict(id=identity,external_match_id=identity,league_id=base.league_id,
                    season_id=base.season_id,home_team_id=base.home_team_id,away_team_id=base.away_team_id,
                    round=1,status='finished',kickoff_at=kickoff,ft_home=2,ft_away=i%2,raw={}))
                snap=dict(match_id=identity,league_id=base.league_id,captured_at=kickoff-timedelta(hours=1),
                    model_version='load-test-v1',picks=picks)
                snapshots.append(snap)
                proof=dict(status='finished',ft_home=2,ft_away=i%2,ht_home=None,ht_away=None,
                    is_final=False,statistics={})
                results.append(dict(match_id=identity,pick_index=0,revision=1,outcome='won' if i%2 else 'lost',
                    actual={'value':2+i%2},evidence=proof,snapshot_hash=snapshot_digest(SimpleNamespace(**snap)),
                    evidence_hash=digest(proof),evaluator_version=VERSION,recorded_at=kickoff+timedelta(hours=3),
                    actor='test',reason='load fixture'))
            await session.execute(insert(Match),matches)
            await session.execute(insert(PredictionSnapshot),snapshots)
            await session.execute(insert(PredictionSettlement),results)
            await session.commit()
        queries=[]
        def count_query(*args):queries.append(1)
        event.listen(db.engine.sync_engine,'before_cursor_execute',count_query)
        start=time.monotonic()
        try:
            large=await refresh(db)
        finally:event.remove(db.engine.sync_engine,'before_cursor_execute',count_query)
        elapsed=time.monotonic()-start
        assert elapsed < 20
        assert len(queries)<80
        print(f'PostgreSQL learning: 2000 added settled picks, {len(queries)} statements in {elapsed:.3f}s')
        async with db.session() as session:
            stored=await session.get(LearningReport,large)
            assert stored.summary['predictions']==2000+len(items)
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


async def source_data_new_session(db):
    async with db.session() as session:return await source_data(session)


async def test_atomic_publish_failure_and_capacity_keep_previous_report(db,monkeypatch):
    await finished(db)
    import src.analytics.learning as learning
    original=learning.publish_head
    async def fail(*args):raise RuntimeError('simulated publish interruption')
    monkeypatch.setattr(learning,'publish_head',fail)
    with pytest.raises(RuntimeError):await refresh(db)
    async with db.session() as session:
        assert await session.scalar(select(func.count(LearningReport.id)))==0
        assert await session.scalar(select(func.count(LearningLesson.id)))==0
    monkeypatch.setattr(learning,'publish_head',original)
    first=await refresh(db)
    monkeypatch.setattr(learning,'MAX_RECORDS',0)
    with pytest.raises(ValueError,match='bounded capacity'):await refresh(db)
    async with db.session() as session:
        assert (await session.get(LearningHead,METHOD)).report_id==first
        assert await session.scalar(select(func.count(LearningReport.id)))==1


@pytest.mark.parametrize('status,coverage_key',[('cancelled','recorded_terminal'),('finished','result_data_pending')])
async def test_missing_and_void_results_never_enter_denominator(db,status,coverage_key):
    target,*_=await seed(db)
    async with db.session() as session:
        match=await session.get(Match,target);match.status=status
        count=len((await session.get(PredictionSnapshot,target)).picks)
        await settle_match(session,match);await session.commit()
    report=await refresh(db)
    async with db.session() as session:
        stored=await session.get(LearningReport,report)
        assert stored.summary['predictions']==0 and stored.summary['success_rate'] is None
        assert stored.coverage[coverage_key]==count


def test_audited_export_is_verified_and_reproducible():
    import json
    from pathlib import Path
    from scripts.learning_report import exported_source,report
    root=Path(__file__).resolve().parents[1]
    export=json.loads((root/'docs/learning-phase-2-source.json').read_text(encoding='utf-8'))
    computed=report(*exported_source(export['records']),export['audit_completed_at_utc'])
    expected=json.loads((root/'docs/learning-phase-2-example.json').read_text(encoding='utf-8'))
    assert computed==expected
    assert computed['summary']['predictions']==120
    assert computed['summary']['observed_wins']==92
    assert computed['coverage']['exclusions']=={'void':1}
    corrupted=deepcopy(export['records'])
    next(r for r in corrupted if r['settlements'])['settlements'][0]['outcome']='wrong'
    with pytest.raises(ValueError):exported_source(corrupted)


@pytest.mark.parametrize('seed',['0','1','7','42','random'])
def test_audited_report_exact_across_hash_seeds_and_input_orders(seed):
    import json,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    code='''
import json,random
from pathlib import Path
from scripts.learning_report import exported_source,report
export=json.loads(Path('docs/learning-phase-2-source.json').read_text(encoding='utf-8'))
rng=random.Random(SEED)
rng.shuffle(export['records'])
for record in export['records']:rng.shuffle(record['settlements'])
items,coverage,fingerprint=exported_source(export['records'])
rng.shuffle(items)
print(json.dumps(report(items,coverage,fingerprint,export['audit_completed_at_utc']),sort_keys=True))
'''.replace('SEED',repr(seed))
    result=subprocess.run([sys.executable,'-c',code],cwd=root,
        env={**os.environ,'PYTHONHASHSEED':seed},capture_output=True,text=True,encoding='utf-8',check=True,timeout=30)
    expected=json.loads((root/'docs/learning-phase-2-example.json').read_text(encoding='utf-8'))
    assert json.loads(result.stdout)==expected
    assert len(expected['lessons'])==55
    assert all(x['payload']['status']=='insufficient_sample' for x in expected['lessons'])


def test_metrics_and_fitted_calibration_do_not_depend_on_row_order():
    import random
    rows=[row(i,p=.7,y=i%2) for i in range(600)]
    expected_metrics=metrics(rows)
    expected_calibration=evaluate_calibration(rows)
    for seed in (0,1,7):
        shuffled=deepcopy(rows);random.Random(seed).shuffle(shuffled)
        assert metrics(shuffled)==expected_metrics
        assert evaluate_calibration(shuffled)==expected_calibration


async def test_worker_keeps_learning_pool_separate_and_cleans_up(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import src.jobs.runner as runner
    import src.jobs.telegram as telegram
    import src.analytics.learning as learning
    seen={};closed=[]
    async def close_main():closed.append('main')
    async def close_learning():closed.append('learning')
    main=SimpleNamespace(close=close_main)
    own=SimpleNamespace(close=close_learning)
    def create(url):return own
    async def work():await asyncio.sleep(.01)
    async def telegram_loop(db):
        seen['telegram']=db
        await asyncio.Event().wait()
    async def learning_loop(db):
        seen['learning']=db
        await asyncio.Event().wait()
    monkeypatch.setattr(runner,'database',main)
    monkeypatch.setattr(runner,'Database',create)
    monkeypatch.setattr(runner,'work',work)
    monkeypatch.setattr(runner,'os',SimpleNamespace(name='nt'))
    monkeypatch.setattr(telegram,'loop',telegram_loop)
    monkeypatch.setattr(learning,'loop',learning_loop)
    await runner.run()
    assert seen=={'telegram':main,'learning':own}
    assert closed==['learning','main']
