import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from src.db import Database
from src.models import Base, League, Season, Team, Match, Bookmaker, Odds1X2, AsianHandicap, AsianTotals, ScraperJob, JobItem
from src.jobs.verified_odds import fill_only, is_complete
from src.scrapers.goaloo.odds import parse_odds
import scripts.verified_odds_backfill as repair


def test_fixture_identity_checks_known_scores_and_time():
    row=dict(external_match_id=1,external_league_id=6,status='finished',home_external_id=2,
             away_external_id=3,kickoff_at=datetime(2025,1,1,tzinfo=timezone.utc),
             ft_home=1,ft_away=0,ht_home=None,ht_away=None)
    assert repair.verify_fixture(row,row,6)
    for key,value in [('ft_home',2),('home_external_id',3),('status','scheduled'),
                      ('external_league_id',7),('external_match_id',2),('kickoff_at',None)]:
        assert not repair.verify_fixture(row,{**row,key:value},6)
    assert not repair.verify_fixture(row,None,6)
    assert repair.attempts('attempts=2;source_error')==2


@pytest.mark.asyncio
async def test_repair_limits_and_capacity(monkeypatch):
    for ids,limit in [([],25),([6,6],25),([315],1),([6,7],26),([6],0)]:
        with pytest.raises(ValueError):
            await repair.run(ids,limit)
    async def used(session):
        return {'database_bytes':650_000_000}
    monkeypatch.setattr(repair,'check_capacity',used)
    with pytest.raises(RuntimeError,match='650 MB'):
        await repair.capacity(None)


@pytest.fixture
async def pg():
    if not os.getenv('TEST_POSTGRES_URL'):
        pytest.skip('TEST_POSTGRES_URL is not configured')
    db=Database(os.environ['TEST_POSTGRES_URL'])
    schema='odds_fill_'+uuid.uuid4().hex
    async with db.engine.begin() as conn:
        await conn.execute(CreateSchema(schema))
    db.engine=db.engine.execution_options(schema_translate_map={None:schema})
    db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.session() as s:
        league=League(external_id=6,name='Poland',country='Poland',enabled=True)
        home=Team(external_id=1,name='Home');away=Team(external_id=2,name='Away')
        s.add_all([league,home,away]);await s.flush()
        season=Season(league_id=league.id,season_name='2025-2026');s.add(season);await s.flush()
        match=Match(external_match_id=100,league_id=league.id,season_id=season.id,round=1,
            home_team_id=home.id,away_team_id=away.id,status='finished',ft_home=1,ft_away=0,
            ht_home=0,ht_away=0,kickoff_at=datetime(2025,1,1,tzinfo=timezone.utc))
        s.add(match)
        s.add_all([Bookmaker(name=n,external_id=i) for n,i in [('Crown',3),('Bet365',8),('Sbobet',31)]])
        s.add(ScraperJob(league_id=league.id,kind='europe_bounded',status='manual_paused',
                        discovery_metrics={'_stage2_admission':{'proof':'preserve'}}))
        await s.commit()
    try:
        yield db
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


def quotes():
    result={}
    for book in ('Crown','Bet365','Sbobet'):
        result[book]={}
        for market,sides in [('1x2',('home','draw','away')),('ah',('line','home','away')),('ou',('line','over','under'))]:
            result[book][market]={f'{stage}_{side}':0.0 if side=='line' else 1.9
                for stage in ('opening','closing') for side in sides}
    return result


@pytest.mark.asyncio
async def test_postgres_atomic_fill_preserves_prices_latest_and_provenance(pg):
    odds=quotes()
    async with pg.session() as s:
        match=await s.scalar(select(Match));book=await s.scalar(select(Bookmaker).where(Bookmaker.name=='Crown'))
        stamp='2024-01-01T00:00:00+00:00'
        s.add(Odds1X2(match_id=match.id,bookmaker_id=book.id,opening_home=1.9,latest_home=9.0,
            raw={'legacy':'keep','_field_observed_at':{'latest_home':stamp,'opening_home':stamp}}))
        s.add(AsianHandicap(match_id=match.id,bookmaker_id=book.id,opening_line=0,opening_home=1.9,
                           latest_line=2,latest_home=9,raw={'_field_lines':{'latest_home':2}}))
        await s.commit()
        mid=match.id
    async with pg.session() as s:
        result=await fill_only(s,mid,100,1,odds)
        assert result==dict(inserted_rows=7,updated_rows=2)
        assert await is_complete(s,mid)
        await s.commit()
    async with pg.session() as s:
        row=await s.scalar(select(Odds1X2).where(Odds1X2.bookmaker_id==book.id))
        assert row.latest_home==9 and row.opening_home==1.9 and row.closing_home==1.9
        assert row.raw['legacy']=='keep'
        assert row.raw['_field_observed_at']['opening_home']==stamp
        assert row.raw['_field_observed_at']['latest_home']==stamp
        assert row.raw['_verified_odds_fill']['external_match_id']==100
        ah=await s.scalar(select(AsianHandicap).where(AsianHandicap.bookmaker_id==book.id))
        assert ah.opening_line==0 and ah.opening_away==1.9 and ah.latest_line==2
        assert ah.raw['_field_lines']['latest_home']==2
        assert ah.raw['_field_lines']['opening_away']==0
        assert await fill_only(s,mid,100,1,odds)==dict(inserted_rows=0,updated_rows=0)
        match=await s.get(Match,mid)
        assert (match.ft_home,match.ft_away,match.ht_home,match.ht_away)==(1,0,0,0)
        assert match.last_scraped_at is None


@pytest.mark.asyncio
async def test_postgres_conflicting_stage_and_unknown_line_cannot_mix_prices(pg):
    async with pg.session() as s:
        match=await s.scalar(select(Match));book=await s.scalar(select(Bookmaker).where(Bookmaker.name=='Crown'))
        s.add(Odds1X2(match_id=match.id,bookmaker_id=book.id,opening_home=2.8,raw={'old':True}))
        s.add(AsianHandicap(match_id=match.id,bookmaker_id=book.id,opening_line=1,opening_home=1.9))
        s.add(AsianTotals(match_id=match.id,bookmaker_id=book.id,opening_over=1.9))
        await s.commit()
        await fill_only(s,match.id,100,1,quotes());await s.commit()
        row=await s.scalar(select(Odds1X2).where(Odds1X2.bookmaker_id==book.id))
        assert row.opening_home==2.8 and row.opening_draw is None and row.closing_draw==1.9
        ah=await s.scalar(select(AsianHandicap).where(AsianHandicap.bookmaker_id==book.id))
        assert ah.opening_line==1 and ah.opening_away is None
        ou=await s.scalar(select(AsianTotals).where(AsianTotals.bookmaker_id==book.id))
        assert ou.opening_line is None and ou.opening_under is None
        assert not await is_complete(s,match.id)


@pytest.mark.asyncio
async def test_postgres_repair_reuses_checkpoint_and_source_gaps_are_terminal(pg,monkeypatch,tmp_path):
    monkeypatch.setattr(repair,'database',pg)
    record=dict(external_id=6,catalog_name='POL D1',country='Poland',name='Poland',
                competition_type='club',schedule_format='league',priority=100,
                latest_season='2025-2026',seasons=['2025-2026'],verified=True,approved=True,error=None)
    report=tmp_path/'report.json';report.write_text(json.dumps({'competitions':[record]}))
    monkeypatch.setattr(repair,'REPORT',report)
    source=dict(external_match_id=100,external_league_id=6,status='finished',home_external_id=1,
        away_external_id=2,kickoff_at=datetime(2025,1,1,tzinfo=timezone.utc),ft_home=1,ft_away=0,ht_home=0,ht_away=0)
    async def schedule(*args):
        assert pg.engine.pool.checkedout()==1  # Only advisory lock; no transaction during HTTP.
        return {}
    async def odds(*args):
        assert pg.engine.pool.checkedout()==1
        data=quotes();data['Sbobet']['ou']['closing_under']=None
        return data
    monkeypatch.setattr(repair,'season_data',schedule)
    monkeypatch.setattr(repair,'validate_identity',lambda *a:None)
    monkeypatch.setattr(repair,'discover_rounds',lambda p:[1])
    monkeypatch.setattr(repair,'parse_matches',lambda *a:[source])
    monkeypatch.setattr(repair,'fetch_odds',odds)
    result=await repair.run([6],25)
    assert result[0]['source_gaps']==1 and result[0]['inserted_rows']==9
    result=await repair.run([6],25)
    assert result[0]['attempted']==0
    async with pg.session() as s:
        assert await s.scalar(select(func.count()).select_from(Match))==1
        assert await s.scalar(select(func.count()).select_from(ScraperJob))==1
        job=await s.scalar(select(ScraperJob))
        assert job.status=='manual_paused'
        assert job.discovery_metrics['_stage2_admission']=={'proof':'preserve'}
        assert job.discovery_metrics['_stage3_verified_odds']['attempted']==1
        assert job.total_matches==1 and job.no_odds_matches==1
        assert (await s.scalar(select(JobItem))).status=='source_unavailable'
