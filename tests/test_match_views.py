from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import shutil
import subprocess
import pytest
from sqlalchemy import select, func
from src.models import League, ScraperJob, Match
from src.jobs.storage import store_match
from src.jobs.worker import initial_backfill_job, enqueue_initial_backfill
from src.scrapers.goaloo.matches import parse_matches
from .test_goaloo import fixture

NOW = datetime(2026, 10, 7, 22, 30, tzinfo=timezone.utc)  # Istanbul: Oct 8, 01:30.


async def seed_views(db, monkeypatch):
    import src.api.routes as routes
    monkeypatch.setattr(routes, 'utcnow', lambda: NOW)
    base = next(parse_matches(fixture('goaloo_league.json'), 1))
    specs = [
        ('before_day', 'finished', datetime(2026,10,7,20,59,tzinfo=timezone.utc)),
        ('day_start', 'finished', datetime(2026,10,7,21,tzinfo=timezone.utc)),
        ('live', 'live', NOW-timedelta(hours=1)),
        ('first', 'first_half', NOW-timedelta(minutes=30)),
        ('half', 'half_time', NOW-timedelta(minutes=20)),
        ('second', 'second_half', NOW-timedelta(minutes=10)),
        ('stale_schedule', 'scheduled', NOW-timedelta(hours=1)),
        ('at_now', 'scheduled', NOW),
        ('tomorrow', 'scheduled', NOW+timedelta(hours=20)),
        ('near', 'scheduled', NOW+timedelta(hours=1)),
        ('far', 'scheduled', NOW+timedelta(days=8)),
        ('cancelled', 'cancelled', NOW+timedelta(hours=2)),
        ('next_day', 'finished', datetime(2026,10,8,21,tzinfo=timezone.utc)),
    ]
    ids={}
    async with db.session() as session:
        league=await session.scalar(select(League))
        for index,(name,status,kickoff) in enumerate(specs):
            data=deepcopy(base);data.update(external_match_id=8000000+index,status=status,kickoff_at=kickoff)
            match=await store_match(session,league.id,'2026-2027',data);ids[name]=match.id
        await session.commit()
    return ids


async def test_live_excludes_finished_and_scheduled(api,db,monkeypatch):
    ids=await seed_views(db,monkeypatch)
    page=(await api.get('/api/matches?view=live')).json()
    assert {m['id'] for m in page['items']}=={ids[k] for k in ['live','first','half','second']}


async def test_today_istanbul_midnight_and_ascending(api,db,monkeypatch):
    ids=await seed_views(db,monkeypatch)
    page=(await api.get('/api/matches?view=today')).json()
    found={m['id'] for m in page['items']}
    assert ids['day_start'] in found and ids['before_day'] not in found and ids['next_day'] not in found
    assert {'scheduled','live','finished'} <= {m['status'] for m in page['items']}
    dates=[m['kickoff_at'] for m in page['items']];assert dates==sorted(dates)
    utc=(await api.get('/api/matches?view=today&display_timezone=UTC')).json()
    assert ids['before_day'] in {m['id'] for m in utc['items']}
    assert (await api.get('/api/matches?display_timezone=not-a-zone')).status_code==400


async def test_upcoming_future_only_window_and_date_extension(api,db,monkeypatch):
    ids=await seed_views(db,monkeypatch)
    page=(await api.get('/api/matches?view=upcoming')).json()
    assert [m['id'] for m in page['items']]==[ids['near'],ids['tomorrow']]
    wide=(await api.get('/api/matches?view=upcoming&upcoming_days=30')).json()
    assert wide['total']==3
    date=(await api.get('/api/matches?view=upcoming&date=2026-10-16')).json()
    assert [m['id'] for m in date['items']]==[ids['far']]


async def test_historical_filters_and_pagination(api,db,monkeypatch):
    ids=await seed_views(db,monkeypatch)
    page=(await api.get('/api/matches?view=history&season=2026-2027&round=1&team=Manchester&league=1&limit=2')).json()
    assert page['total']==3
    assert [m['id'] for m in page['items']]==[ids['next_day'],ids['day_start']]
    next_page=(await api.get('/api/matches?view=history&limit=2&offset=2')).json()
    assert next_page['total']==3 and [m['id'] for m in next_page['items']]==[ids['before_day']]
    assert (await api.get('/api/matches?view=invalid')).status_code==422


async def test_date_filter_uses_display_day(api,db,monkeypatch):
    ids=await seed_views(db,monkeypatch)
    page=(await api.get('/api/matches?date=2026-10-08')).json()
    found={m['id'] for m in page['items']}
    assert ids['day_start'] in found and ids['before_day'] not in found and ids['next_day'] not in found


async def test_auto_backfill_storage_does_not_duplicate_jobs(db):
    async with db.session() as session:first=await initial_backfill_job(session)
    async with db.session() as session:second=await initial_backfill_job(session)
    assert first.id==second.id and first.start_year==2024
    async with db.session() as session:
        assert await session.scalar(select(func.count(ScraperJob.id)))==1
        job=await session.get(ScraperJob,first.id);job.status='failed';await session.commit()
    async with db.session() as session:assert (await initial_backfill_job(session)).id==first.id


async def test_auto_backfill_existing_matches_and_disabled_flag(db,monkeypatch):
    await seed_views(db,monkeypatch)
    async with db.session() as session:assert await initial_backfill_job(session) is None
    assert await enqueue_initial_backfill(db,enabled=False) is None
    assert await enqueue_initial_backfill(db,enabled=True) is None  # SQLite never auto-scrapes.


@pytest.mark.skipif(not shutil.which('node'),reason='Node required for browser odds helper tests')
def test_browser_odds_opening_closing_and_latest_prematch():
    script = r'''
      const assert=require('node:assert/strict');
      const {stage,movement,asianMovement}=require('./src/api/dashboard-odds.js');
      const market={opening:{home:2.15},latest:{home:1.92},closing:{home:1.88},raw:{r:{u:'999'}}};
      assert.equal(movement({status:'finished'},market,'home'),'2.15 → 1.88');
      assert.equal(movement({status:'scheduled'},market,'home'),'2.15 → 1.92');
      assert.equal(stage({status:'scheduled'},market),'latest');
      const missing={...market,closing:{home:null}};
      assert.equal(movement({status:'finished'},missing,'home'),'2.15 → —');
      assert.equal(movement({status:'scheduled'},missing,'home'),'2.15 → 1.92');
      const asian={opening:{home:1.8,line:-1,away:2.0},latest:{home:1.9,line:-0.75,away:1.9},closing:{home:null,line:null,away:null}};
      assert.equal(asianMovement({status:'scheduled'},asian,['home','line','away']),'1.8 / -1 / 2 → 1.9 / -0.75 / 1.9');
    '''
    subprocess.run(['node','-e',script],check=True,capture_output=True)
