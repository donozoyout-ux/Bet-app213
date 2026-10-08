import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import pytest
from sqlalchemy import select, func, text
from src.models import League, Match, ScraperJob, Bookmaker
from src.db import Database
from src.scrapers.goaloo.competitions import parse_catalog, select_targets, verified_snapshot, upsert_competitions, validate_identity, Competition, verify_competition
from src.scrapers.goaloo.client import SourceError
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.seasons import discover_seasons
from src.jobs.worker import queue_missing_competitions
from src.jobs.storage import store_match


def fixture(name):
    return json.loads((Path(__file__).parent / 'fixtures' / name).read_text(encoding='utf-8'))


def test_catalog_dynamic_ids_and_national_selection():
    source=Path('tests/fixtures/goaloo_competitions.js').read_text(encoding='utf-8')
    entries=parse_catalog(source)
    targets=select_targets(entries)
    assert len(targets)==30
    assert sum(item.competition_type=='club' for item in targets)==15
    assert targets[0].external_id==36
    assert next(item for item in targets if item.catalog_name=='INT FRL').external_id==1366
    assert not any(item.catalog_name in {'INT CF','UEFA CL'} for item in targets)
    # Numeric mappings really come from the source, not from a parallel ID list.
    modified=source.replace('36,ENG PR,','936,ENG PR,')
    assert select_targets(parse_catalog(modified))[0].external_id==936
    with pytest.raises(SourceError):select_targets(entries+entries)
    with pytest.raises(SourceError):parse_catalog('arr[0] = alert(1);')


@pytest.mark.parametrize('name,expected_id,kind',[('goaloo_cup.json',75,'cup'),('goaloo_subleague.json',21,'league'),('goaloo_euro.json',67,'cup')])
def test_real_cup_subleague_and_archive_identity(name,expected_id,kind):
    payload=fixture(name)
    catalog=next(c for c in verified_snapshot() if c['external_id']==expected_id)
    competition=Competition(**catalog)
    season=catalog['latest_season']
    assert validate_identity(competition,payload,season)
    rounds=discover_rounds(payload)
    rows=[match for n in rounds for match in parse_matches(payload,n)]
    assert rows and all(row['external_league_id']==expected_id for row in rows)
    assert all(row['round_label'] and row['stage_key'] for row in rows)
    payload['LeagueInfo'][0]=999999
    with pytest.raises(SourceError):validate_identity(competition,payload,season)


async def test_calendar_seasons_and_overlapping_national_tournament():
    class Client:
        async def get(self,*args):return {'SeasonList':['2023-2025','2024','2026','2022','2021-2023']}
    assert await discover_seasons(Client(),1,2024)==['2024','2026']
    assert await discover_seasons(Client(),1,2024,overlap=True)==['2021-2023','2023-2025','2024','2026']


async def test_verified_upsert_and_tier_queue_are_idempotent(db):
    records=verified_snapshot()
    async with db.session() as session:
        original=await session.scalar(select(League).where(League.external_id==36))
        original_id=original.id
        await upsert_competitions(session,records)
        await upsert_competitions(session,records)
        assert await session.scalar(select(func.count(League.id)))==30
        assert (await session.scalar(select(League).where(League.external_id==36))).id==original_id
        jobs=await queue_missing_competitions(session)
        assert len(jobs)==30
        await session.commit()
        assert await queue_missing_competitions(session)==[]
        assert [job.priority for job in jobs]==list(range(1,31))
        assert all(job.start_year==2024 for job in jobs)
    async with db.session() as session:
        with pytest.raises(ValueError):await upsert_competitions(session,[{**records[0],'verified':False}])


async def test_all_competitions_upcoming_chronological_and_enabled_only(api,db,monkeypatch):
    import src.api.routes as routes
    now=datetime(2026,10,8,12,tzinfo=timezone.utc)
    monkeypatch.setattr(routes,'utcnow',lambda:now)
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot())
        club=await session.scalar(select(League).where(League.external_id==36))
        national=await session.scalar(select(League).where(League.external_id==75))
        disabled=await session.scalar(select(League).where(League.external_id==31))
        disabled.enabled=False
        template=next(parse_matches(fixture('goaloo_cup.json'),1))
        for index,(league,hours) in enumerate([(club,48),(national,24),(club,24*120),(disabled,1)]):
            data={**template,'external_match_id':template['external_match_id']+10000+index,'external_league_id':league.external_id,'kickoff_at':now+timedelta(hours=hours),'status':'scheduled','ht_home':None,'ht_away':None,'ft_home':None,'ft_away':None}
            await store_match(session,league.id,'2026',data)
        await session.commit()
    response=(await api.get('/api/matches?view=upcoming&include_odds=true')).json()
    assert [row['competition_type'] for row in response['items']]==['national','club']
    assert response['items'][0]['home_team']==template['home_name']
    assert not response['upcoming_expanded']
    assert (await api.get(f'/api/matches?view=upcoming&league={club.id}')).json()['total']==1
    assert not any(row['external_id']==31 for row in (await api.get('/api/leagues')).json())


async def test_empty_seven_day_window_uses_next_match_day(api,db,monkeypatch):
    import src.api.routes as routes
    now=datetime(2026,10,8,12,tzinfo=timezone.utc)
    monkeypatch.setattr(routes,'utcnow',lambda:now)
    async with db.session() as session:
        league=await session.scalar(select(League))
        template=next(parse_matches(fixture('goaloo_cup.json'),1))
        for index,days in enumerate([120,10,10]):
            data={**template,'external_match_id':template['external_match_id']+index,'kickoff_at':now+timedelta(days=days,hours=index),'status':'scheduled'}
            await store_match(session,league.id,'2026',data)
        await session.commit()
    page=(await api.get('/api/matches?view=upcoming')).json()
    assert page['total']==2 and page['upcoming_expanded']
    assert page['items'][0]['kickoff_at'] < page['items'][1]['kickoff_at']
    # Pagination retains the same next-day window rather than widening to later months.
    assert (await api.get('/api/matches?view=upcoming&limit=1&offset=1')).json()['total']==2
    assert not (await api.get('/api/matches?view=upcoming&date=2026-10-09')).json()['items']


async def test_national_worker_keeps_real_stages_and_excludes_pre_2024(db,monkeypatch):
    import src.jobs.worker as worker
    payload=fixture('goaloo_euro.json')
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot())
        await session.commit()
    monkeypatch.setattr(worker,'database',db)
    async def seasons(*args,**kwargs):return ['2023-2024']
    async def data(*args,**kwargs):return payload
    monkeypatch.setattr(worker,'discover_seasons',seasons)
    monkeypatch.setattr(worker,'season_data',data)
    job=await worker.enqueue('backfill',league_external_id=67)
    await worker.discover(job.id,None)
    async with db.session() as session:
        matches=(await session.scalars(select(Match))).all()
        assert matches and all(match.kickoff_at.year>=2024 for match in matches)
        assert all(match.stage_key and match.round_label for match in matches)


async def test_additive_migration_preserves_existing_league(db):
    from src.migrations import migrate_competitions
    async with db.engine.begin() as conn:
        # Existing data survives repeated migration runs.
        before=(await conn.execute(text('SELECT id,name FROM leagues'))).all()
        await migrate_competitions(conn)
        await migrate_competitions(conn)
        assert (await conn.execute(text('SELECT id,name FROM leagues'))).all()==before


async def test_upgrade_legacy_schema_keeps_match_ids_scores_and_league(tmp_path):
    db=Database(f'sqlite+aiosqlite:///{tmp_path / "legacy.db"}')
    try:
        async with db.engine.begin() as conn:
            await conn.execute(text('CREATE TABLE leagues (id INTEGER PRIMARY KEY,external_id INTEGER UNIQUE,name VARCHAR(150),country VARCHAR(80),source VARCHAR(30))'))
            await conn.execute(text("INSERT INTO leagues VALUES (42,36,'English Premier League','England','goaloo')"))
            await conn.execute(text('CREATE TABLE matches (id INTEGER PRIMARY KEY,external_match_id INTEGER UNIQUE,league_id INTEGER,season_id INTEGER,round INTEGER,kickoff_at DATETIME,home_team_id INTEGER,away_team_id INTEGER,status VARCHAR(30),ht_home INTEGER,ht_away INTEGER,ft_home INTEGER,ft_away INTEGER,raw JSON,odds_complete BOOLEAN,last_scraped_at DATETIME,created_at DATETIME,updated_at DATETIME)'))
            await conn.execute(text("INSERT INTO matches (id,external_match_id,league_id,round,status,ft_home,ft_away,raw,odds_complete) VALUES (77,2590898,42,1,'finished',1,0,'{}',1)"))
        await db.initialize()
        await db.initialize()
        async with db.session() as session:
            match=await session.get(Match,77)
            assert (match.external_match_id,match.league_id,match.ft_home,match.ft_away,match.odds_complete)==(2590898,42,1,0,True)
            league=await session.get(League,42)
            assert league.competition_type=='club' and league.enabled
            assert await session.scalar(select(func.count(League.id)))==1
    finally:await db.close()


async def test_authenticated_all_league_update_queue(api,db,monkeypatch):
    import src.api.routes as routes
    async with db.session() as session:
        await upsert_competitions(session,verified_snapshot())
        await session.commit()
    monkeypatch.setattr(routes,'settings',replace(routes.settings,scraper_token='unit-test-token'))
    assert (await api.post('/api/scraper/update-all')).status_code==401
    first=await api.post('/api/scraper/update-all',headers={'Authorization':'Bearer unit-test-token'})
    second=await api.post('/api/scraper/update-all',headers={'Authorization':'Bearer unit-test-token'})
    assert first.status_code==second.status_code==202
    assert len(first.json())==30
    assert [job['id'] for job in first.json()]==[job['id'] for job in second.json()]
