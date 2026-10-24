"""Dedicated Neon reader. Never initializes schema, workers or application DB."""
from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def source_engine(url):
    parsed=make_url(url)
    if parsed.drivername not in {'postgres','postgresql','postgresql+psycopg','postgresql+asyncpg'}:
        raise ValueError('PostgreSQL reader required')
    if parsed.query.get('sslmode')!='verify-full':
        raise ValueError('Archive requires sslmode=verify-full')
    # libpq preserves channel_binding=require and verifies hostname/CA.
    return create_async_engine(parsed.set(drivername='postgresql+psycopg'),
        poolclass=NullPool,echo=False,hide_parameters=True,
        connect_args={'connect_timeout':10,'application_name':'owned-audit-reader'})


async def validate_reader(connection):
    # Table AND column grants, inherited ownership, database/schema creation and
    # elevated role membership are checked. A read-only transaction alone cannot
    # establish that the supplied credential is a dedicated read-only principal.
    unsafe=await connection.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM pg_roles r
          WHERE pg_has_role(current_user,r.oid,'MEMBER')
            AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls))
        OR has_database_privilege(current_user,current_database(),'CREATE')
        OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
            AND has_schema_privilege(current_user,n.oid,'CREATE'))
        OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','v','m','f','S')
          AND (pg_has_role(current_user,c.relowner,'MEMBER')
            OR CASE WHEN c.relkind='S' THEN has_sequence_privilege(current_user,c.oid,'USAGE,UPDATE')
               ELSE has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
                 OR has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE') END))
        """))
    if unsafe:raise ValueError('Dedicated SELECT-only source credential required')


@asynccontextmanager
async def snapshot(url):
    engine=source_engine(url)
    try:
        async with engine.connect() as connection:
            connection=await connection.execution_options(isolation_level='REPEATABLE READ')
            async with connection.begin():
                await connection.execute(text('SET TRANSACTION READ ONLY'))
                await connection.execute(text("SET LOCAL statement_timeout='60000'"))
                await connection.execute(text("SET LOCAL lock_timeout='2000'"))
                await connection.execute(text("SET LOCAL search_path=public,pg_catalog"))
                await validate_reader(connection)
                yield connection
    finally:
        await engine.dispose()
