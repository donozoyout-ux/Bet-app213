from contextlib import asynccontextmanager
import logging
import os
import time
import asyncio
from weakref import WeakKeyDictionary
from sqlalchemy import event
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.config import settings
from src.models import Base, League, Bookmaker
from src.migrations import migrate_competitions

log = logging.getLogger(__name__)


class AdmissionTimeout(Exception):
    pass


class Database:
    def __init__(self, url: str, *, role=None):
        self.role = role or ('scraper' if os.getenv('BETAPP_PROCESS_ROLE') == 'scraper' else 'web')
        if self.role not in {'web','scraper','learning','settlement','ai'}:
            raise ValueError('Unknown database process role')
        self._lanes = WeakKeyDictionary()
        self.metrics = dict(checkouts=0, checked_out=0, high_water=0, max_hold_ms=0,
                            slow_holds=0, pool_timeouts=0, admission_timeouts=0, max_admission_ms=0)
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+asyncpg://' + url.split('://', 1)[1]
        if url and not url.startswith(('postgresql+asyncpg://', 'sqlite+aiosqlite://')):
            raise ValueError('DATABASE_URL must use PostgreSQL asyncpg (SQLite aiosqlite is supported for tests)')
        if settings.app_env == 'production' and url.startswith('sqlite'):
            raise ValueError('Production requires PostgreSQL')
        options = {}
        if url.startswith('postgresql+asyncpg://'):
            # Neon uses libpq channel_binding=require. asyncpg does not support it;
            # use psycopg3/libpq when channel binding must be enforced, never strip it.
            parsed = make_url(url)
            sslmode = parsed.query.get('sslmode')
            if sslmode is not None and sslmode not in ('require', 'verify-ca', 'verify-full'):
                raise ValueError('DATABASE_URL sslmode must require TLS')
            channel_binding = parsed.query.get('channel_binding')
            if channel_binding is not None and channel_binding not in ('require', 'prefer', 'disable'):
                raise ValueError('Unsupported DATABASE_URL channel_binding option')
            use_libpq = channel_binding == 'require'
            if use_libpq:
                if sslmode is None:
                    raise ValueError('channel_binding=require needs sslmode=require or stricter')
                url = parsed.set(drivername='postgresql+psycopg')
            else:
                # asyncpg takes SSL policy in connect_args, not in the URL.
                url = parsed.difference_update_query(['sslmode', 'channel_binding'])
            # Reserve HTTP connections; a scraper process has only its lock + one writer.
            worker = self.role != 'web'
            size = 5 if self.role == 'web' else 2 if self.role == 'scraper' else 1
            options = dict(pool_size=size, max_overflow=0,
                           pool_timeout=2, pool_recycle=300,
                           connect_args={'timeout': 5, 'command_timeout': 15,
                               'server_settings': {'application_name': 'betapp-' + self.role,
                                   'statement_timeout': '12000', 'lock_timeout': '2000'}})
            if use_libpq:
                # psycopg/libpq honors sslmode and channel_binding from the URL.
                # Neon PgBouncer pooler rejects arbitrary startup `options=-c ...`.
                # Keep bounded app-side pool/timeouts; do not send session GUCs at startup.
                options['connect_args'] = {'connect_timeout': 5,
                    'application_name': 'betapp-' + self.role}
            elif sslmode is not None:
                options['connect_args']['ssl'] = sslmode
        self.engine = create_async_engine(url, pool_pre_ping=True, **options) if url else None
        if self.engine:
            @event.listens_for(self.engine.sync_engine, 'checkout')
            def checkout(connection, record, proxy):
                record.info['held_since'] = time.monotonic()
                self.metrics['checkouts'] += 1
                self.metrics['checked_out'] += 1
                self.metrics['high_water'] = max(self.metrics['high_water'], self.metrics['checked_out'])

            @event.listens_for(self.engine.sync_engine, 'checkin')
            def checkin(connection, record):
                started = record.info.pop('held_since', None)
                if started is None: return
                ms = (time.monotonic() - started) * 1000
                self.metrics['checked_out'] = max(0, self.metrics['checked_out'] - 1)
                self.metrics['max_hold_ms'] = max(self.metrics['max_hold_ms'], round(ms, 1))
                if ms >= 1000:
                    self.metrics['slow_holds'] += 1
                    log.warning('[DB_POOL] role=%s hold_ms=%.1f checked_out=%s', self.role, ms, self.metrics['checked_out'])

            @event.listens_for(self.engine.sync_engine, 'invalidate')
            def invalidated(connection, record, exc):
                checkin(connection, record)

            @event.listens_for(self.engine.sync_engine, 'before_cursor_execute')
            def query_start(conn, cursor, statement, parameters, context, executemany):
                context.betapp_started = time.monotonic()

            @event.listens_for(self.engine.sync_engine, 'after_cursor_execute')
            def query_end(conn, cursor, statement, parameters, context, executemany):
                elapsed = time.monotonic() - context.betapp_started
                from src.query_profile import record
                record(statement,elapsed)
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
            from src.prediction_integrity import install_prediction_guards
            await install_prediction_guards(conn)
            await migrate_competitions(conn)
            if not (await conn.execute(select(League.id).where(League.external_id == 36))).first():
                await conn.execute(League.__table__.insert().values(external_id=36, name='English Premier League', country='England', source='goaloo'))
            if conn.dialect.name == 'postgresql':
                from src.scrapers.goaloo.competitions import verified_snapshot, upsert_competitions
                async with async_sessionmaker(conn, expire_on_commit=False)() as session:
                    records=verified_snapshot()
                    if settings.app_env == 'production':
                        from src.scrapers.goaloo.competitions import PRODUCTION_LEAGUE_IDS, apply_production_scope, approved_europe_snapshot, reviewed_europe_snapshot
                        records=[r for r in records if r['external_id'] in PRODUCTION_LEAGUE_IDS]
                        records.extend(approved_europe_snapshot())
                        records.extend(reviewed_europe_snapshot())
                    await upsert_competitions(session, records)
                    if settings.app_env == 'production':await apply_production_scope(session)
            for external_id, name in [(3, 'Crown'), (8, 'Bet365'), (31, 'Sbobet')]:
                if not (await conn.execute(select(Bookmaker.id).where(Bookmaker.name == name))).first():
                    await conn.execute(Bookmaker.__table__.insert().values(name=name, external_id=external_id))
        self.ready = True
        log.info('[DB] schema initialized and seeds verified')

    @asynccontextmanager
    async def request_slot(self, lane):
        """Partition the EXISTING five web connections: three general, two metadata.

        Admission occurs before any SQL. It is not a second or larger pool.
        SQLite and background jobs retain their established session behavior.
        """
        if not self.engine or self.engine.dialect.name != 'postgresql' or self.role != 'web' or self.engine.pool.size() < 3:
            yield lambda: None
            return
        size = self.engine.pool.size()
        lanes = self._lanes.setdefault(asyncio.get_running_loop(),
            {'metadata':asyncio.BoundedSemaphore(2), 'general':asyncio.BoundedSemaphore(size-2)})
        gate = lanes[lane]
        started = time.monotonic()
        try:
            async with asyncio.timeout(2):
                await gate.acquire()
        except TimeoutError as exc:
            self.metrics['admission_timeouts'] += 1
            raise AdmissionTimeout() from exc
        self.metrics['max_admission_ms'] = max(self.metrics['max_admission_ms'], round((time.monotonic()-started)*1000, 1))
        released = False
        def release():
            nonlocal released
            if not released:
                gate.release(); released = True
        try:
            yield release
        finally:
            release()

    def pool_health(self):
        return dict(role=self.role, **self.metrics,
            pool_size=self.engine.pool.size() if self.engine and hasattr(self.engine.pool,'size') else None,
            overflow=max(0,self.engine.pool.overflow()) if self.engine and hasattr(self.engine.pool,'overflow') else None)

    @asynccontextmanager
    async def session(self):
        if not self.sessions:
            raise RuntimeError('DATABASE_URL is not configured')
        from sqlalchemy.exc import TimeoutError as PoolTimeout
        try:
            async with self.sessions() as session:
                yield session
        except PoolTimeout:
            self.metrics['pool_timeouts'] += 1
            raise

    async def close(self):
        if self.engine:
            await self.engine.dispose()
        self.ready = False


database = Database(settings.database_url)
