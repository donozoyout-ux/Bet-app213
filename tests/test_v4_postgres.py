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
from src.models import PredictionBoard,Match
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
            data['unused_evidence']=[{'index':i,'detail':str(uuid.uuid4())*8} for i in range(1000)]
            board.payload=data;await s.commit()
        queries=[];payload_statement=[]
        def collect(conn,cursor,statement,*args):
            queries.append(statement)
            if 'publication_json' in statement and not payload_statement:payload_statement.append((statement,args[0]))
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
        assert sum('prediction_boards.payload' in q for q in queries)==1
        event.remove(db.engine.sync_engine,'before_cursor_execute',collect)
        from src.api.main import app
        import src.api.routes as routes
        from src.api.routes import session_dependency
        monkeypatch.setattr(routes,'database',db);monkeypatch.setattr(routes,'utcnow',lambda:NOW)
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
                async with db.session() as s:
                    m=await s.get(Match,warm[0][0]['match']['id']);m.status='live';await s.commit()
                assert len((await api.get('/api/predictions')).json()['items'])==3
                monkeypatch.setattr(routes,'utcnow',lambda:NOW+timedelta(hours=1))
                assert (await api.get('/api/predictions')).json()['items']==[]
        finally:app.dependency_overrides.clear()
        async with db.engine.connect() as conn:
            statement,parameters=payload_statement[0]
            plan=(await conn.exec_driver_sql('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+statement,parameters)).scalar()
        if isinstance(plan,str):plan=json.loads(plan)
        print('V4_PUBLICATION_BENCHMARK '+json.dumps(dict(basis='isolated PostgreSQL synthetic evidence fixture; not deployed production',cold_ms=round(cold_ms,3),warm_ms=round(warm_ms,3),cold_sql=cold_sql,warm_sql=warm_sql,concurrent_readers=8,concurrent_payload_reads=1,http_total_ms=round(http_ms,3),responses=responses,plan=plan)))
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True))
        await db.close()
