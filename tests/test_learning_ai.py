"""All provider traffic is MockTransport; never access a real NVIDIA account."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import os
import uuid
import httpx
import pytest
from sqlalchemy import select,func,text
from src.analytics import learning_ai as ai
from src.models import LearningAIExplanation,LearningAITask,LearningAIQuota,LearningAIGate,LearningLesson,LearningReport,utcnow
from tests.test_learning_browser import edition

SECRET='nvapi-TEST-SECRET-NOT-A-REAL-KEY'
CFG=ai.AIConfig(True,SECRET,'test/configurable-model',100)


def success(request):
    payload=json.loads(request.content)
    pack=json.loads(payload['messages'][1]['content'])
    assert request.url==ai.URL and request.headers['authorization']=='Bearer '+SECRET
    assert SECRET not in json.dumps(payload)
    assert 'title' not in pack['evidence'] and len(request.content)<10000
    return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({k:v[0] for k,v in pack['allowed'].items()},ensure_ascii=False)}}]})


async def only_general(db):
    # Use an isolated test report with one valid lesson for exact idempotency checks.
    report=await edition(db,empty=True)
    data=json.loads(open('docs/learning-phase-2-example.json',encoding='utf-8').read())
    async with db.session() as session:
        lesson=LearningLesson(report_id=report,scope_key='overall:all',payload=data['lessons'][0]['payload'])
        session.add(lesson);await session.commit()
    return report


async def package(db):
    await only_general(db)
    async with db.session() as session:
        return ai.evidence(await session.scalar(select(LearningReport)),await session.scalar(select(LearningLesson)))


@pytest.mark.parametrize('field,value',[
    ('enabled',False),('key',''),('model',''),('daily_limit',0),('daily_limit',1001),('model',SECRET),('key','bad\nheader')])
async def test_disabled_has_no_database_or_network_access(field,value):
    from dataclasses import replace
    assert await ai.run_once(None,replace(CFG,**{field:value}))=='disabled'
    assert SECRET not in repr(CFG)


@pytest.mark.parametrize('bad',[
    'Başarı yüzde yüz; kesin kazanç.', 'Örneklem yeterli; yüksek güven vardır.',
    'Gerçek başarı %99,9 ve Brier 0.001.', '<img src=x onerror=alert(1)>',
    'Başarı yüzde doksan dokuzdur.', 'https://evil.example/', SECRET,'x'*4000])
async def test_rejects_numeric_unsupported_and_unsafe_claims(db,bad):
    p=await package(db);data={k:v[0] for k,v in ai.choices(p).items()};data['reliability']=bad
    with pytest.raises(ValueError):ai.validate(json.dumps(data),p)
    with pytest.raises(ValueError):ai.validate('{}',p)
    with pytest.raises(ValueError):ai.validate('null',p)
    with pytest.raises(ValueError):ai.validate('{"observed":"x","observed":"y"}',p)


async def test_end_to_end_persistence_read_only_api_revisions_and_secrets(db,api,monkeypatch,caplog):
    report=await only_general(db);calls=[]
    def provider(request):calls.append(request);return success(request)
    monkeypatch.setattr(ai,'config',lambda:CFG)
    before=(await api.get('/api/learning/lessons')).json()
    assert before['items'][0]['ai']['status']=='not_prepared' and not calls
    assert await ai.run_once(db,CFG,httpx.MockTransport(provider))=='completed'
    assert await ai.run_once(db,CFG,httpx.MockTransport(provider))=='idle'
    assert len(calls)==1
    output=(await api.get('/api/learning/lessons')).json()['items'][0]['ai']
    assert output['status']=='ready' and output['report_id']==report
    assert output['facts']['predictions']==120 and output['facts']['observed_success']==92/120
    assert output['model']==CFG.model and output['prompt_version']==ai.PROMPT_VERSION
    assert 'doğrulanmış bir ders değildir' in output['sections']['conclusion']
    async with db.session() as session:
        stored=await session.scalar(select(LearningAIExplanation))
        assert stored.report_id==report and stored.evidence_hash
        assert await session.scalar(select(func.count(LearningAIExplanation.id)))==1
        assert (await session.get(LearningAIQuota,utcnow().date().isoformat())).requests==1
        dumped=json.dumps({k:v for k,v in stored.__dict__.items() if not k.startswith('_')},default=str)
        assert SECRET not in dumped and SECRET not in caplog.text
    # New report editions get new immutable narratives; the old one remains addressable.
    new=await edition(db,empty=True,fingerprint='c'*64)
    async with db.session() as session:
        old=await session.scalar(select(LearningLesson).where(LearningLesson.report_id==report))
        payload=deepcopy(old.payload);payload['data_fingerprint']='c'*64
        session.add(LearningLesson(report_id=new,scope_key=old.scope_key,payload=payload))
        await session.commit()
    assert await ai.run_once(db,CFG,httpx.MockTransport(provider))=='completed'
    assert (await api.get(f'/api/learning/lessons?report_id={report}')).json()['items'][0]['ai']==output
    async with db.session() as session:assert await session.scalar(select(func.count(LearningAIExplanation.id)))==2
    for endpoint in ('summary','coverage','statistics','lessons','reports'):
        response=await api.get('/api/learning/'+endpoint)
        assert response.status_code==200 and SECRET not in response.text
    assert len(calls)==2
    monkeypatch.setattr(ai,'config',lambda:ai.AIConfig())
    assert (await api.get('/api/learning/lessons')).json()['items'][0]['ai']['status']=='disabled'


@pytest.mark.parametrize('status,code',[(429,'rate_limited'),(500,'provider_error'),(401,'provider_error'),(302,'provider_error')])
async def test_provider_error_backoff_retry_quota_no_secret(db,status,code,caplog):
    await only_general(db);now=utcnow();calls=[]
    def failed(request):calls.append(1);return httpx.Response(status,headers={'retry-after':'600'},text=SECRET)
    transport=httpx.MockTransport(failed)
    assert await ai.run_once(db,CFG,transport,now)==code
    assert await ai.run_once(db,CFG,transport,now+timedelta(seconds=1))=='backoff'
    async with db.session() as session:
        task=await session.scalar(select(LearningAITask));assert SECRET not in str(task.__dict__)
        assert task.error_code==code
    if status in (429,500):
        assert await ai.run_once(db,CFG,transport,now+timedelta(seconds=601))==code
        assert await ai.run_once(db,CFG,transport,now+timedelta(seconds=1202))==code
        assert await ai.run_once(db,CFG,transport,now+timedelta(seconds=3600))=='idle'
        assert len(calls)==3
    else:assert len(calls)==1
    assert SECRET not in caplog.text


async def test_timeout_budget_day_rollover_and_crash_ambiguity(db):
    await only_general(db);now=utcnow().replace(hour=12)
    def timed_out(request):raise httpx.ReadTimeout(SECRET,request=request)
    cfg=ai.AIConfig(True,SECRET,CFG.model,1)
    assert await ai.run_once(db,cfg,httpx.MockTransport(timed_out),now)=='timeout'
    assert await ai.run_once(db,cfg,httpx.MockTransport(success),now+timedelta(hours=1))=='quota'
    assert await ai.run_once(db,cfg,httpx.MockTransport(success),now+timedelta(days=1))=='completed'
    async with db.session() as session:
        task=await session.scalar(select(LearningAITask));task.status='running';task.updated_at=now-timedelta(hours=1)
        await session.commit()
    assert await ai.run_once(db,cfg,httpx.MockTransport(success),now+timedelta(days=2))=='idle'
    async with db.session() as session:assert (await session.scalar(select(LearningAITask))).status=='uncertain'


@pytest.mark.parametrize('kind',['oversized','truncated','invalid_json','secret_output'])
async def test_provider_payload_rejection(db,kind):
    await only_general(db)
    def provider(request):
        if kind=='oversized':return httpx.Response(200,content=b'x'*20000)
        if kind=='invalid_json':return httpx.Response(200,text='not JSON '+SECRET)
        response=json.loads(success(request).content)
        if kind=='truncated':response['choices'][0]['finish_reason']='length'
        else:response['choices'][0]['message']['content']=SECRET
        return httpx.Response(200,json=response)
    assert await ai.run_once(db,CFG,httpx.MockTransport(provider))=='rejected_output'
    async with db.session() as session:assert await session.scalar(select(func.count(LearningAIExplanation.id)))==0


async def test_supported_and_inconsistent_evidence_fail_closed(db):
    p=await package(db)
    assert not p['supported']
    async with db.session() as session:
        report=await session.scalar(select(LearningReport));lesson=await session.scalar(select(LearningLesson))
        lesson.payload=deepcopy(lesson.payload);lesson.payload['status']='supported_candidate'
        with pytest.raises(ValueError):ai.evidence(report,lesson)
        lesson.payload['evidence'].update(effective_matches=120,minimum_effective_matches=100,gap_interval=[.1,.2])
        lesson.payload['matches']=120
        valid=ai.evidence(report,lesson);assert valid['supported']
        assert 'adaydır' in ai.choices(valid)['reliability'][0]
        await session.rollback()


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='PostgreSQL not configured')
async def test_postgres_global_lock_quota_and_immutable_narrative():
    from src.db import Database
    from sqlalchemy.schema import CreateSchema,DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.exc import DBAPIError
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='ai_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema});db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await db.initialize();await only_general(db)
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def provider(request):calls.append(1);entered.set();await release.wait();return success(request)
        cfg=ai.AIConfig(True,SECRET,CFG.model,1)
        task=asyncio.create_task(ai.run_once(db,cfg,httpx.MockTransport(provider)))
        await asyncio.wait_for(entered.wait(),10)
        try:assert await ai.run_once(db,cfg,httpx.MockTransport(provider))=='busy'
        finally:release.set()
        assert await task=='completed' and len(calls)==1
        assert await ai.run_once(db,cfg,httpx.MockTransport(provider))=='idle'
        async with db.session() as session:
            assert await session.scalar(select(func.count(LearningAIExplanation.id)))==1
            for op in ('UPDATE learning_ai_explanations SET model=model','DELETE FROM learning_ai_explanations'):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():await session.execute(text(op.replace('learning_ai_explanations',schema+'.learning_ai_explanations')))
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True))
        await db.close()

async def test_prompt_version_creates_new_immutable_edition(db,monkeypatch):
    await only_general(db);transport=httpx.MockTransport(success)
    assert await ai.run_once(db,CFG,transport)=='completed'
    monkeypatch.setattr(ai,'PROMPT_VERSION','test-prompt-v2')
    assert await ai.run_once(db,CFG,transport)=='completed'
    assert await ai.run_once(db,CFG,transport)=='idle'
    async with db.session() as session:
        assert await session.scalar(select(func.count(LearningAIExplanation.id)))==2
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            async with session.begin_nested():await session.execute(text('DELETE FROM learning_ai_explanations'))


async def test_cancelled_provider_is_not_blindly_retried(db):
    await only_general(db);entered=asyncio.Event()
    async def waiting(request):entered.set();await asyncio.Future()
    now=utcnow();task=asyncio.create_task(ai.run_once(db,CFG,httpx.MockTransport(waiting),now))
    await asyncio.wait_for(entered.wait(),5);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert await ai.run_once(db,CFG,httpx.MockTransport(success),now+timedelta(minutes=11))=='idle'
    async with db.session() as session:
        assert (await session.scalar(select(LearningAITask))).status=='uncertain'
        assert (await session.scalar(select(LearningAIQuota))).requests==1
        assert await session.scalar(select(func.count(LearningAIExplanation.id)))==0


async def test_real_settlement_correction_gets_new_ai_evidence(db):
    from tests.test_learning import finished
    from src.analytics.learning import refresh
    from src.analytics.settlements import correct_settlement,decisions
    from src.analytics.performance import settle
    from src.models import Match,PredictionSnapshot
    match_id=await finished(db);first=await refresh(db)
    transport=httpx.MockTransport(success)
    assert await ai.run_once(db,CFG,transport)=='completed'
    async with db.session() as session:
        original=await session.scalar(select(LearningAIExplanation));old=deepcopy(original.facts)
        match=await session.get(Match,match_id);match.ft_home=0;match.ft_away=0
        snapshot=await session.get(PredictionSnapshot,match_id);frozen=await decisions(session,match_id)
        index=next(i for i,r in frozen.items() if settle(match,None,snapshot.picks[i])[0]!=r.outcome)
        await correct_settlement(session,match_id,index,expected_revision=1,actor='test',reason='verified correction')
        await session.commit()
    second=await refresh(db);assert first!=second
    assert await ai.run_once(db,CFG,transport)=='completed'
    async with db.session() as session:
        reports=(await session.scalars(select(LearningAIExplanation).order_by(LearningAIExplanation.id))).all()
        assert len(reports)==2 and reports[0].report_id==first and reports[1].report_id==second
        assert reports[0].facts==old and reports[1].facts!=old

async def test_runner_enabled_ai_has_separate_pool_and_cleanup(monkeypatch):
    from types import SimpleNamespace
    import src.jobs.runner as runner
    import src.jobs.telegram as telegram
    import src.analytics.learning as learning
    import src.jobs.settlement as settlement
    seen={};closed=[];pools=[]
    async def close_main():closed.append('main')
    main=SimpleNamespace(close=close_main)
    def create(url, *, role):
        number=len(pools)
        async def close():closed.append(number)
        db=SimpleNamespace(close=close,role=role);pools.append(db);return db
    async def work():await asyncio.sleep(.01)
    async def wait(name,db):seen[name]=db;await asyncio.Event().wait()
    monkeypatch.setattr(runner,'database',main);monkeypatch.setattr(runner,'Database',create)
    monkeypatch.setattr(runner,'work',work);monkeypatch.setattr(runner,'os',SimpleNamespace(name='nt'))
    monkeypatch.setattr(telegram,'loop',lambda db:wait('telegram',db))
    monkeypatch.setattr(learning,'loop',lambda db:wait('learning',db))
    monkeypatch.setattr(settlement,'loop',lambda db:wait('settlement',db))
    monkeypatch.setattr(ai,'loop',lambda db:wait('ai',db));monkeypatch.setattr(ai,'config',lambda:CFG)
    await runner.run()
    assert seen=={'telegram':main,'learning':pools[0],'settlement':pools[1],'ai':pools[2]}
    assert [db.role for db in pools]==['learning','settlement','ai']
    assert closed==[2,0,1,'main']


@pytest.mark.parametrize('field,value',[('data_fingerprint','wrong'),('method_version','wrong')])
async def test_source_provenance_mismatch_rejected_without_provider(db,field,value):
    await only_general(db)
    async with db.session() as session:
        report=await session.scalar(select(LearningReport));lesson=await session.scalar(select(LearningLesson))
        lesson.payload=deepcopy(lesson.payload);lesson.payload[field]=value
        with pytest.raises(ValueError):ai.evidence(report,lesson)
        await session.rollback()

@pytest.mark.parametrize('secret',[SECRET,'nvapi-test.secret.suffix','nonprefix.private.secret'])
async def test_debug_transport_logs_and_tracebacks_cannot_echo_key(db,caplog,secret):
    import logging
    await only_general(db)
    caplog.set_level(logging.DEBUG)
    def provider(request):
        for name in ('httpx','httpcore.http11','httpcore.http2','httpcore.connection'):
            logging.getLogger(name).debug('response headers: authorization=%s',secret)
            try:raise ValueError(secret)
            except ValueError:logging.getLogger(name).debug('provider debug',exc_info=True)
        return httpx.Response(500,headers={'x-provider-echo':secret},text=secret)
    assert await ai.run_once(db,ai.AIConfig(True,secret,CFG.model,10),httpx.MockTransport(provider))=='provider_error'
    assert secret not in caplog.text and '[NVIDIA_KEY_REDACTED]' in caplog.text

@pytest.mark.parametrize('observed,word',[(.6,'altında'),(.9,'üstünde'),(.8,'aynı')])
async def test_direction_claim_is_gated_by_backend_evidence(db,observed,word):
    p=await package(db);p['facts']['expected_success']=.8;p['facts']['observed_success']=observed
    allowed=ai.choices(p);assert word in allowed['actual'][0]
    result={k:v[0] for k,v in allowed.items()}
    assert ai.validate(json.dumps(result),p)==result
    other=deepcopy(p);other['facts']['observed_success']=.6 if observed>.8 else .9
    result['actual']=ai.choices(other)['actual'][0]
    with pytest.raises(ValueError):ai.validate(json.dumps(result),p)
