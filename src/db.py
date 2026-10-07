from contextlib import asynccontextmanager
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.config import settings
from src.models import Base, League, Bookmaker


class Database:
    def __init__(self, url: str):
        if url.startswith(('postgres://', 'postgresql://')):
            url = 'postgresql+asyncpg://' + url.split('://', 1)[1]
        if url and not url.startswith(('postgresql+asyncpg://', 'sqlite+aiosqlite://')):
            raise ValueError('DATABASE_URL must use PostgreSQL asyncpg (SQLite aiosqlite is supported for tests)')
        if settings.app_env == 'production' and url.startswith('sqlite'):
            raise ValueError('Production requires PostgreSQL')
        self.engine = create_async_engine(url, pool_pre_ping=True) if url else None
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False) if self.engine else None
        self.ready = False

    @property
    def configured(self):
        return self.engine is not None

    async def initialize(self):
        if not self.engine:
            return
        async with self.engine.begin() as conn:
            if conn.dialect.name == 'postgresql':
                await conn.execute(text('SELECT pg_advisory_xact_lock(213001)'))
            await conn.run_sync(Base.metadata.create_all)
            if not (await conn.execute(select(League.id).where(League.external_id == 36))).first():
                await conn.execute(League.__table__.insert().values(external_id=36, name='English Premier League', country='England', source='goaloo'))
            for external_id, name in [(3, 'Crown'), (8, 'Bet365'), (31, 'Sbobet')]:
                if not (await conn.execute(select(Bookmaker.id).where(Bookmaker.name == name))).first():
                    await conn.execute(Bookmaker.__table__.insert().values(name=name, external_id=external_id))
        self.ready = True

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
