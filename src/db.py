from contextlib import asynccontextmanager
import logging
import os
import time
from sqlalchemy import event
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.config import settings
from src.models import Base, League, Bookmaker
from src.migrations import migrate_competitions

log = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str):
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+asyncpg://' + url.split('://', 1)[1]
        if url and not url.startswith(('postgresql+asyncpg://', 'sqlite+aiosqlite://')):
            raise ValueError('DATABASE_URL must use PostgreSQL asyncpg (SQLite aiosqlite is supported for tests)')
        if settings.app_env == 'production' and url.startswith('sqlite'):
            raise ValueError('Production requires PostgreSQL')
        options = {}
        if url.startswith('postgresql+asyncpg://'):
            # Reserve HTTP connections; a scraper process has only its lock + one writer.
            worker = os.getenv('BETAPP_PROCESS_ROLE') == 'scraper'
            options = dict(pool_size=2 if worker else 5, max_overflow=0,
                           pool_timeout=2, pool_recycle=300,
                           connect_args={'timeout': 5, 'command_timeout': 15,
                               'server_settings': {'application_name': 'betapp-scraper' if worker else 'betapp-web',
                                   'statement_timeout': '12000', 'lock_timeout': '2000'}})
        self.engine = create_async_engine(url, pool_pre_ping=True, **options) if url else None
        if self.engine:
            @event.listens_for(self.engine.sync_engine, 'before_cursor_execute')
            def query_start(conn, cursor, statement, parameters, context, executemany):
                context.betapp_started = time.monotonic()

            @event.listens_for(self.engine.sync_engine, 'after_cursor_execute')
            def query_end(conn, cursor, statement, parameters, context, executemany):
                elapsed = time.monotonic() - context.betapp_started
                if elapsed >= .25:
                    # Never log SQL parameter values or connection credentials.
                    log.warning('[DB] slow query operation=%s duration_ms=%.1f', statement.split()[0], elapsed * 1000)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False) if self.engine else None
        self.ready = False

    @property
    def configured(self):
        return self.engine is not None

    async def initialize(self):
        if not self.engine:
            return
        async with self.engine.begin() as conn:
            log.info('[DB] connection established')
            if conn.dialect.name == 'postgresql':
                await conn.execute(text('SELECT pg_advisory_xact_lock(213001)'))
            await conn.run_sync(Base.metadata.create_all)
            await migrate_competitions(conn)
            if not (await conn.execute(select(League.id).where(League.external_id == 36))).first():
                await conn.execute(League.__table__.insert().values(external_id=36, name='English Premier League', country='England', source='goaloo'))
            if conn.dialect.name == 'postgresql':
                from src.scrapers.goaloo.competitions import verified_snapshot, upsert_competitions
                async with async_sessionmaker(conn, expire_on_commit=False)() as session:
                    records=verified_snapshot()
                    if settings.app_env == 'production':
                        from src.scrapers.goaloo.competitions import PRODUCTION_LEAGUE_IDS, apply_production_scope
                        records=[r for r in records if r['external_id'] in PRODUCTION_LEAGUE_IDS]
                    await upsert_competitions(session, records)
                    if settings.app_env == 'production':await apply_production_scope(session)
            for external_id, name in [(3, 'Crown'), (8, 'Bet365'), (31, 'Sbobet')]:
                if not (await conn.execute(select(Bookmaker.id).where(Bookmaker.name == name))).first():
                    await conn.execute(Bookmaker.__table__.insert().values(name=name, external_id=external_id))
        self.ready = True
        log.info('[DB] schema initialized and seeds verified')

    @asynccontextmanager
    async def session(self):
        if not self.sessions:
            raise RuntimeError('DATABASE_URL is not configured')
        async with self.sessions() as session:
            yield session

    async def close(self):
        if self.engine:
            await self.engine.dispose()
        self.ready = False


database = Database(settings.database_url)
