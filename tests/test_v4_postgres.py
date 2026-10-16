"""Real PostgreSQL: publication cache single-flight, SQL plan and HTTP evidence."""
import asyncio,json,os,time,uuid
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select,event,text
from sqlalchemy.schema import CreateSchema,DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker
from httpx import AsyncClient,ASGITransport
from src.db import Database
from src.models import PredictionBoard,Match,MarketOdds
from src.analytics.board_cache import read_board,payload_cache
from tests.test_automatic_telegram import many_matches,NOW


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_pg_concurrent_publication_reads_fresh_guards_and_http(monkeypatch):
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='v4_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await many_matches(db,4)
        async with db.session() as s:
            board=await s.scalar(select(PredictionBoard));data=deepcopy(board.payload)
            data['unused_evidence']=[{'index':i,'detail':str(uuid.uuid4())*8} for i in range(32000)]
            from src.analytics.publications import publish_board
            await publish_board(s,board,NOW,candidate_payload=data);await s.commit()
        # Same isolated publication: measure the former whole-document cast
        # against direct split columns. Production EXPLAIN ANALYZE is forbidden.
        from src.models import BoardReadPublication
        async with db.session() as s:
            statement=select(PredictionBoard.payload)
            from sqlalchemy.dialects.postgresql import JSONB
            data=PredictionBoard.payload.cast(JSONB)
            legacy=select(data['items'],data['availability'],data['evaluated'])
            start=time.perf_counter();old=(await s.execute(legacy)).one()
            legacy_ms=(time.perf_counter()-start)*1000
            start=time.perf_counter();new=(await s.execute(select(BoardReadPublication.items,BoardReadPublication.availability,BoardReadPublication.evaluated))).one()
            split_ms=(time.perf_counter()-start)*1000
            assert old==new
            print('SPLIT_PUBLICATION_BENCHMARK '+json.dumps(dict(legacy_cast_ms=round(legacy_ms,3),split_ms=round(split_ms,3),basis='same isolated ~10MB evidence publication; no production speed claim')))
        queries=[];payload_statement=[]
        def collect(conn,cursor,statement,*args):
            queries.append(statement)
            if 'board_read_publications.items' in statement and not payload_statement:payload_statement.append((statement,args[0]))
        event.listen(db.engine.sync_engine,'before_cursor_execute',collect)
        async def read():
            async with db.session() as s:return await read_board(s,None,None,None,NOW)
        start=time.perf_counter();cold=await read();cold_ms=(time.perf_counter()-start)*1000
        cold_sql=len(queries);queries.clear()
        start=time.perf_counter();warm=await read();warm_ms=(time.perf_counter()-start)*1000
        assert cold[0]==warm[0] and len(warm[0])==4
        assert len(queries)==2 and not any('prediction_boards.payload' in q for q in queries)
        warm_sql=len(queries)
        payload_cache.databases.clear();queries.clear()
        await asyncio.gather(*(read() for _ in range(8)))
        assert sum('board_read_publications.items' in q for q in queries)==1
        assert not any('CAST(' in q or 'prediction_boards.payload' in q for q in queries)
        event.remove(db.engine.sync_engine,'before_cursor_execute',collect)
        # Explain while these bound publication values are still current. The
        # later version-change test intentionally invalidates the old version.
        async with db.engine.connect() as conn:
            statement,parameters=payload_statement[0]
            plan=(await conn.exec_driver_sql('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+statement,parameters)).scalar()
        if isinstance(plan,str):plan=json.loads(plan)
        assert plan[0]['Plan']['Actual Rows']==1
        from src.analytics.publications import publish_board
        async with db.session() as s:
            projection=await s.scalar(select(BoardReadPublication))
            await s.delete(projection);await s.commit()
        async with db.session() as s:
            assert (await read_board(s,None,None,None,NOW))[0]==[]
            assert s.info['prediction_snapshot']['status']=='warming'
        async with db.session() as s:
            b=await s.scalar(select(PredictionBoard))
            await publish_board(s,b,NOW);await s.commit()
        assert len((await read())[0])==4
        async with db.session() as s:
            b=await s.scalar(select(PredictionBoard));changed=deepcopy(b.payload)
            changed['items'][0]['recommendations'][0]['probability']-=.001
            await publish_board(s,b,NOW,candidate_payload=changed);await s.commit()
        assert (await read())[0][0]['recommendations'][0]['probability']!=warm[0][0]['recommendations'][0]['probability']
        from src.api.main import app
        import src.api.routes as routes
        from src.api.routes import session_dependency
        monkeypatch.setattr(routes,'database',db);monkeypatch.setattr(routes,'utcnow',lambda:NOW)
        from src.analytics.publications import publish_match_analyses
        from src.models import BoardHead,PublishedMatchAnalysis
        async with db.session() as s:
            target=await s.get(Match,warm[0][0]['match']['id'])
            row=(await s.execute(routes.match_query()[0].where(Match.id==target.id))).first()
            result=await routes.compute_statistics(s,row[0],routes.match_response(row),NOW)
            head=await s.get(BoardHead,target.league_id);board=await s.get(PredictionBoard,target.league_id)
            await publish_match_analyses(s,{target.id:result},head.version,board.model_version,NOW,board.expires_at)
            await s.commit();detail_id=target.id
        calculate=routes.compute_statistics
        async def legacy_statistics(match_id,session):
            row=(await session.execute(routes.match_query()[0].where(Match.id==match_id))).first()
            return await calculate(session,row[0],routes.match_response(row),NOW)
        async def forbidden(*a,**k):raise AssertionError('HTTP calculated a model')
        monkeypatch.setattr(routes,'compute_statistics',forbidden)
        async def session():
            async with db.session() as s:yield s
        app.dependency_overrides[session_dependency]=session
        try:
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as api:
                responses=[];start=time.perf_counter()
                for path in ['/api/predictions?limit=8','/api/predictions/best?limit=20']*2:
                    r=await api.get(path);responses.append(dict(path=path,status=r.status_code,rendered=len(r.json().get('items',[]))))
                http_ms=(time.perf_counter()-start)*1000
                assert all(r['status']==200 and r['rendered']>0 for r in responses)
                # Same URL, DB, clock and response schema: reproduce the old
                # foreground calculation only in this isolated benchmark.
                from src.analytics import service
                service._cache.clear();analysis_queries=[]
                def count_analysis(conn,cursor,statement,*args):analysis_queries.append(statement)
                current_reader=routes.read_published_statistics
                monkeypatch.setattr(routes,'read_published_statistics',legacy_statistics)
                event.listen(db.engine.sync_engine,'before_cursor_execute',count_analysis)
                start=time.perf_counter();legacy=await api.get(f'/api/matches/{detail_id}/statistics')
                analysis_before_ms=(time.perf_counter()-start)*1000;analysis_before_sql=len(analysis_queries)
                monkeypatch.setattr(routes,'read_published_statistics',current_reader)
                service._cache.clear();analysis_queries.clear()
                start=time.perf_counter();detail=await api.get(f'/api/matches/{detail_id}/statistics')
                analysis_after_ms=(time.perf_counter()-start)*1000;analysis_after_sql=len(analysis_queries)
                event.remove(db.engine.sync_engine,'before_cursor_execute',count_analysis)
                assert legacy.status_code==200 and legacy.json()['prediction']==detail.json()['prediction']
                assert analysis_after_sql<analysis_before_sql
                assert detail.status_code==200 and detail.json()['publication']['status']=='current'
                assert (await api.get(f'/api/matches/{detail_id}/statistics')).json()['publication']==detail.json()['publication']
                from tests.odds_fixture import move_observations
                async with db.session() as s:
                    for price in await s.scalars(select(MarketOdds)):move_observations(price,NOW-timedelta(seconds=901))
                    await s.commit()
                unpriced=(await api.get('/api/predictions')).json()
                assert len(unpriced['items'])==4 and unpriced['cache']['verified_price_matches']==0
                async with db.session() as s:
                    m=await s.get(Match,warm[0][0]['match']['id']);m.status='live';await s.commit()
                assert len((await api.get('/api/predictions')).json()['items'])==3
                monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(hours=1))
                assert (await api.get('/api/predictions')).json()['items']==[]
        finally:app.dependency_overrides.clear()
        print('V4_PUBLICATION_BENCHMARK '+json.dumps(dict(basis='isolated PostgreSQL synthetic evidence fixture; not deployed production',cold_ms=round(cold_ms,3),warm_ms=round(warm_ms,3),cold_sql=cold_sql,warm_sql=warm_sql,concurrent_readers=8,concurrent_payload_reads=1,http_total_ms=round(http_ms,3),responses=responses,analysis_before_ms=round(analysis_before_ms,3),analysis_after_ms=round(analysis_after_ms,3),analysis_before_sql=analysis_before_sql,analysis_after_sql=analysis_after_sql,plan=plan)))
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True))
        await db.close()
