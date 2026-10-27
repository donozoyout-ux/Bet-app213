"""Disposable loopback PostgreSQL verification, never a production restore."""
from pathlib import Path
import tempfile
import uuid
from sqlalchemy import text, DateTime, select, func
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.models import Base, Match
from .history_format import parse, timestamp, raw_records
from .history_restore import restore


def verification_engine(url):
    parsed = make_url(url)
    if parsed.host not in {'localhost','127.0.0.1','::1'} or parsed.database not in {'history_verify','betapp_test'}:
        raise ValueError('Restore requires disposable loopback history_verify/betapp_test PostgreSQL')
    if not parsed.drivername.startswith('postgresql'): raise ValueError('PostgreSQL verifier required')
    return create_async_engine(parsed.set(drivername='postgresql+asyncpg'), hide_parameters=True,
        connect_args={'timeout':5,'command_timeout':30})


async def verify_postgres(url, catalog, chunks):
    # Validate exact chunk bytes, eligibility, explicit provenance and FK closure
    # before any SQL is constructed in the isolated PostgreSQL service.
    with tempfile.TemporaryDirectory(prefix='history-verify-') as root:
        import asyncio
        task=asyncio.create_task(asyncio.to_thread(restore,Path(root)/'check.sqlite',catalog,chunks))
        try: await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    engine=verification_engine(url)
    schema='hist_restore_'+uuid.uuid4().hex
    try:
        # All DDL and rows are rolled back, including on a smoke-test failure.
        async with engine.connect() as conn:
            transaction=await conn.begin()
            try:
                await conn.execute(text("SET LOCAL timezone='UTC'"))
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                scoped=await conn.execution_options(schema_translate_map={None:schema})
                await scoped.run_sync(Base.metadata.create_all)
                await conn.execute(text(f'CREATE TABLE "{schema}".archive_bytes (name text PRIMARY KEY, value bytea NOT NULL)'))
                # Dependency ordering is independent of chunk ordering.
                records=[r for raw in chunks for r in raw_records(raw)]
                for table in Base.metadata.sorted_tables:
                    for name,row in records:
                        if name != table.name: continue
                        # PostgreSQL decodes the original JSON text, preserving
                        # arbitrary precision JSON numbers in source payloads.
                        verified=await conn.scalar(text(f'''WITH restored AS (
                            INSERT INTO "{schema}"."{name}" SELECT * FROM json_populate_record(NULL::"{schema}"."{name}",CAST(:row AS json)) RETURNING *)
                            SELECT row_to_json(restored)::jsonb = CAST(:row AS jsonb) FROM restored'''),{'row':row})
                        if verified is not True:raise ValueError('PostgreSQL relational roundtrip mismatch')
                for meta,raw in zip(catalog['chunks'],chunks,strict=True):
                    await conn.execute(text(f'INSERT INTO "{schema}".archive_bytes VALUES (:name,:value)'),{'name':meta['name'],'value':raw})
                    if await conn.scalar(text(f'SELECT value FROM "{schema}".archive_bytes WHERE name=:name'),{'name':meta['name']}) != raw:
                        raise ValueError('PostgreSQL byte roundtrip mismatch')
                for name,count in catalog['counts'].items():
                    if await scoped.scalar(select(func.count()).select_from(Base.metadata.tables[name])) != count:
                        raise ValueError('PostgreSQL count mismatch')
                async with async_sessionmaker(bind=scoped,expire_on_commit=False)() as session:
                    from src.api.match_page import dossier
                    from src.analytics.prediction_audit import audit
                    from src.api.routes import odds_data
                    from src.models import utcnow
                    ids=list(await session.scalars(select(Match.id).order_by(Match.id)))
                    for mid in ids:
                        match=await session.get(Match,mid)
                        if (await dossier(session,mid))['match']['id'] != mid: raise ValueError('Dossier smoke failed')
                        if (await audit(session,match,utcnow()))['match_id'] != mid: raise ValueError('Audit smoke failed')
                        await odds_data(session,mid)
                return dict(postgres_restore=True,read_through=True,exact_bytes=True,
                            counts=catalog['counts'],generation=catalog['generation'])
            finally:
                await transaction.rollback()
    finally: await engine.dispose()
