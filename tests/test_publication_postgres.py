"""Isolated PostgreSQL EXPLAIN and before/after read workload benchmark."""
import os,json,uuid,time
from datetime import timedelta
import pytest
from sqlalchemy import text,event,insert
from sqlalchemy.schema import CreateSchema,DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.db import Database
from src.models import DailyCouponPublication
from src.analytics.publications import publish_daily,read_daily
from src.analytics.saved_coupons import automatic,detail
from tests.test_daily_pool import pool_fixture,NOW


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_URL'),reason='TEST_POSTGRES_URL is not configured')
async def test_postgres_publication_plan_durability_and_query_reduction():
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='publication_'+uuid.uuid4().hex
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize();await pool_fixture(db,4)
        counts=[]
        def count(*args):counts.append(1)
        event.listen(db.engine.sync_engine,'before_cursor_execute',count)
        start=time.perf_counter()
        async with db.session() as session:
            row=await automatic(session,NOW,display_limit=4)
            before=await detail(session,row);await session.commit()
        before_ms=(time.perf_counter()-start)*1000;before_count=len(counts)
        event.remove(db.engine.sync_engine,'before_cursor_execute',count)
        async with db.session() as session:
            published=await publish_daily(session,NOW);await session.commit()
            await session.execute(insert(DailyCouponPublication),[
                dict(day=(NOW-timedelta(days=i)).date().isoformat(),strategy='balanced',input_revision='test',
                     version='test',checked_at=NOW,payload={'items':[]}) for i in range(1,2001)])
            await session.commit()
        counts=[];event.listen(db.engine.sync_engine,'before_cursor_execute',count)
        start=time.perf_counter()
        async with db.session() as session:
            after=await read_daily(session,NOW)
        after_ms=(time.perf_counter()-start)*1000;after_count=len(counts)
        event.remove(db.engine.sync_engine,'before_cursor_execute',count)
        assert after['id']==published['id']==before['id']
        assert after_count<=3 and after_count<before_count
        async with db.engine.connect() as conn:
            await conn.execute(text(f'ANALYZE "{schema}".daily_coupon_publications'))
            plan=await conn.scalar(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM \"{schema}\".daily_coupon_publications WHERE day='2026-10-08' AND strategy='balanced'"))
        if isinstance(plan,str):plan=json.loads(plan)
        report=dict(basis='isolated synthetic PostgreSQL fixtures',before_ms=round(before_ms,3),
            after_ms=round(after_ms,3),before_sql=before_count,after_sql=after_count,plan=plan)
        print('PUBLICATION_BENCHMARK '+json.dumps(report,default=str))
        assert plan[0]['Plan']['Actual Rows']==1
        assert 'Index' in plan[0]['Plan']['Node Type']
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
