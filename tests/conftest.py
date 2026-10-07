import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from src.db import Database
from src.api.main import app
from src.api.routes import session_dependency


@pytest_asyncio.fixture
async def db(tmp_path):
    db = Database(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def api(db, monkeypatch):
    import src.api.routes as routes
    import src.jobs.worker as worker
    monkeypatch.setattr(routes, 'database', db)
    monkeypatch.setattr(worker, 'database', db)
    async def session():
        async with db.session() as session:
            yield session
    app.dependency_overrides[session_dependency] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client
    app.dependency_overrides.clear()
