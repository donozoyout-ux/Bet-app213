from datetime import datetime, timezone
from dataclasses import replace
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from src.api.main import app
from src.models import League, Bookmaker, Match, Odds1X2
from src.jobs.storage import store_match, store_odds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import parse_odds
from .test_goaloo import fixture
import asyncio
from contextlib import asynccontextmanager
import pytest


def test_database_independent_boot(monkeypatch):
    from src.db import Database
    import src.api.main as main
    import src.api.routes as routes
    db = Database('')
    monkeypatch.setattr(main,'database',db)
    monkeypatch.setattr(routes,'database',db)
    with TestClient(app) as client:
        assert client.get('/health').json()['status'] == 'ok'
        assert client.get('/').status_code == 200
        assert client.get('/dashboard.js').status_code == 200
        assert client.get('/api/status').json()['database'] == 'unconfigured'
        assert client.get('/api/matches').status_code == 503


async def test_database_initialization_idempotent(db):
    await db.initialize()
    async with db.session() as session:
        assert await session.scalar(select(func.count(League.id))) == 1
        assert await session.scalar(select(func.count(Bookmaker.id))) == 3


async def seed(db):
    async with db.session() as session:
        league = await session.scalar(select(League))
        matches = []
        for data in parse_matches(fixture('goaloo_league.json'),1):
            matches.append(await store_match(session,league.id,'2024-2025',data))
        await store_odds(session,matches[0],parse_odds(fixture('goaloo_odds.json'),True),True)
        await session.commit()
        return matches[0].id


async def test_real_match_queries_and_detail(api,db):
    match_id = await seed(db)
    leagues = (await api.get('/api/leagues')).json()
    assert leagues[0]['external_id'] == 36
    assert (await api.get(f'/api/leagues/{leagues[0]["id"]}/seasons')).json()[0]['season_name'] == '2024-2025'
    page = (await api.get('/api/matches?team=Manchester%20United&season=2024-2025&round=1&date=2024-08-16&status=finished&bookmaker=Crown&include_odds=true')).json()
    assert page['total'] == 1
    assert len(page['items'][0]['odds']) == 9
    assert page['items'][0]['kickoff_at'].startswith('2024-08-16T19:00:00')
    detail = (await api.get(f'/api/matches/{match_id}')).json()
    assert detail['ft_home'] == 1 and detail['ht_home'] == 0
    assert len(detail['odds']) == 9
    assert len((await api.get(f'/api/matches/{match_id}/odds')).json()) == 9
    assert len((await api.get('/api/bookmakers')).json()) == 3
    status = (await api.get('/api/status')).json()
    assert status['total_matches'] == 10 and status['total_odds'] == 9
    assert (await api.get('/api/matches/999')).status_code == 404
    assert (await api.get('/api/matches?limit=10000')).status_code == 422
    assert (await api.get('/api/matches?bookmaker=Unibet')).status_code == 400
    assert (await api.get('/api/matches?limit=2&offset=2')).json()['total'] == 10


async def test_upsert_no_duplicates_or_erased_verified_prices(db):
    match_id = await seed(db)
    await seed(db)
    async with db.session() as session:
        assert await session.scalar(select(func.count(Match.id))) == 10
        assert await session.scalar(select(func.count(Odds1X2.id))) == 3
        match = await session.get(Match,match_id)
        odds = parse_odds(fixture('goaloo_odds.json'),True)
        odds['Crown']['1x2']['opening_home'] = None
        await store_odds(session,match,odds,True)
        crown = await session.scalar(select(Bookmaker).where(Bookmaker.name=='Crown'))
        row = await session.scalar(select(Odds1X2).where(Odds1X2.match_id==match_id,Odds1X2.bookmaker_id==crown.id))
        assert row.opening_home == 1.53


async def test_job_authorization_validation_and_status(api,db,monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes,'settings',replace(routes.settings,scraper_token='unit-test-token'))
    assert (await api.post('/api/scraper/backfill',json={})).status_code == 401
    response = await api.post('/api/scraper/backfill',json={},headers={'Authorization':'Bearer unit-test-token'})
    assert response.status_code == 202
    job = response.json()
    assert job['status']=='queued'
    assert (await api.get(f'/api/scraper/jobs/{job["id"]}')).json()['id']==job['id']
    assert (await api.get('/api/scraper/status')).json()['status']=='running'
    assert (await api.post('/api/scraper/update',json={},headers={'Authorization':'Bearer unit-test-token'})).status_code == 409
    assert (await api.get('/api/scraper/jobs/999')).status_code==404


async def test_slow_database_does_not_delay_liveness_or_dashboard(monkeypatch):
    import src.api.main as main
    import src.api.routes as routes
    from httpx import AsyncClient, ASGITransport
    class SlowDatabase:
        configured=True
        ready=False
        async def initialize(self): await asyncio.Event().wait()
        async def close(self): pass
    db=SlowDatabase()
    monkeypatch.setattr(main,'database',db)
    monkeypatch.setattr(routes,'database',db)
    assert main.application is main.app
    async def check():
        async with main.lifespan(app):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                assert (await client.get('/health')).status_code==200
                assert (await client.get('/')).status_code==200
                assert (await client.get('/dashboard.js')).status_code==200
                assert (await client.get('/api/matches')).status_code==503
    await asyncio.wait_for(check(),timeout=2)
