from copy import deepcopy
from types import SimpleNamespace
import pytest
from scripts.europe_bounded_history import selected_records
from src.jobs.europe_capacity import check_capacity, STOP_BYTES
from src.scrapers.goaloo.matches import expand_schedule_rows


def test_reviewed_catalog_keeps_new_leagues_import_only():
    from src.scrapers.goaloo.competitions import reviewed_europe_snapshot, PRODUCTION_LEAGUE_IDS
    rows = reviewed_europe_snapshot()
    assert {r['external_id'] for r in rows} == {3,5,6,7,22,26,27,29,32,37,133,137}
    assert {r['external_id'] for r in rows if r['external_id'] in PRODUCTION_LEAGUE_IDS} == {5,29,6,7}
    assert 137 not in PRODUCTION_LEAGUE_IDS


def test_only_reviewed_leagues_and_small_batches():
    row = dict(external_id=7, approved=True, verified=True, error=None)
    assert selected_records([7], {'competitions':[row]}) == [row]
    for ids in ([], [7,7], [7,8,9,10], [99]):
        with pytest.raises(ValueError):
            selected_records(ids, {'competitions':[row]})
    for key in ('approved', 'verified'):
        bad = {**row, key:False}
        with pytest.raises(ValueError):
            selected_records([7], {'competitions':[bad]})


@pytest.mark.asyncio
async def test_capacity_stops_at_threshold_and_fails_closed():
    class Session:
        bind = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        async def scalar(self, query):
            return self.used
    session = Session()
    session.used = STOP_BYTES-1
    assert (await check_capacity(session))['database_bytes'] == STOP_BYTES-1
    session.used = STOP_BYTES
    with pytest.raises(RuntimeError, match='paused'):
        await check_capacity(session)
    session.bind = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'))
    with pytest.raises(RuntimeError, match='PostgreSQL'):
        await check_capacity(session)


def test_real_championship_two_leg_shape_preserves_fixture_ids():
    first = [2575929,37,-1,'2024-05-12 19:00',34,56,'0-0','0-0']
    second = [2575931,37,-1,'2024-05-17 03:00',56,34,'4-0','3-0']
    tie = [34,56,0,4,first,second]
    assert list(expand_schedule_rows([tie])) == [first,second]
    malformed = deepcopy(tie)
    malformed[5][4] = 999
    assert list(expand_schedule_rows([malformed])) == [malformed]
    assert list(expand_schedule_rows([first])) == [first]


@pytest.mark.asyncio
async def test_postgres_bounded_import_resumes_without_duplicate_or_score_rewrite(monkeypatch, tmp_path):
    import os, uuid, json
    if not os.getenv('TEST_POSTGRES_URL'):
        pytest.skip('TEST_POSTGRES_URL is not configured')
    from pathlib import Path
    from sqlalchemy import select, func
    from sqlalchemy.schema import CreateSchema, DropSchema
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.db import Database
    from src.models import Match, League, JobItem, Odds1X2
    from src.scrapers.goaloo.client import SourceError
    from src.scrapers.goaloo.odds import parse_odds
    import scripts.europe_bounded_history as importer
    db = Database(os.environ['TEST_POSTGRES_URL'])
    schema = 'europe_' + uuid.uuid4().hex
    fixture_dir = Path(__file__).with_name('fixtures')
    payload = json.loads((fixture_dir/'goaloo_league.json').read_text(encoding='utf-8'))
    payload['LeagueInfo'][0] = 7
    payload['LeagueInfo'][7] = 'DEN SASL'
    for rows in payload['ScheduleList'].values():
        for row in rows:
            row[1] = 7
    record = dict(external_id=7, catalog_name='DEN SASL', country='Denmark',
        name='Denmark Superliga', competition_type='club', schedule_format='league',
        priority=100, latest_season='2024-2025', seasons=['2024-2025'],
        verified=True, approved=True, error=None, verified_at='2026-10-17T10:00:00+00:00')
    report = tmp_path/'review.json'
    report.write_text(json.dumps({'competitions':[record]}),encoding='utf-8')
    async def schedule(*args): return payload
    async def odds(*args):
        return parse_odds(json.loads((fixture_dir/'goaloo_odds.json').read_text(encoding='utf-8')), True)
    async def unavailable(*args): raise SourceError('Not published')
    async def available(*args): return None
    monkeypatch.setattr(importer, 'database', db)
    monkeypatch.setattr(importer, 'REPORT', report)
    monkeypatch.setattr(importer, 'season_data', schedule)
    monkeypatch.setattr(importer, 'fetch_odds', odds)
    monkeypatch.setattr(importer, 'fetch_statistics', unavailable)
    try:
        async with db.engine.begin() as conn: await conn.execute(CreateSchema(schema))
        db.engine = db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions = async_sessionmaker(db.engine, expire_on_commit=False)
        await db.initialize()
        await importer.run([7], 1, recent_first=True)
        async with db.session() as session:
            assert await session.scalar(select(func.count(Match.id))) == 1
            match = await session.scalar(select(Match))
            from src.scrapers.goaloo.matches import parse_matches
            from src.scrapers.goaloo.rounds import discover_rounds
            source_rows=[m for section in discover_rounds(payload) for m in parse_matches(payload,section,[])]
            assert match.external_match_id==max(source_rows,key=lambda m:m['kickoff_at'])['external_match_id']
            match.ft_home = 99  # A subsequent trusted correction must survive retry.
            assert (await session.scalar(select(JobItem))).status == 'failed'
            await session.commit()
        monkeypatch.setattr(importer, 'fetch_statistics', available)
        await importer.run([7], 1, recent_first=True)
        async with db.session() as session:
            assert await session.scalar(select(func.count(Match.id))) == 1
            assert (await session.scalar(select(Match))).ft_home == 99
            assert (await session.scalar(select(JobItem))).status == 'completed'
            assert not await session.scalar(select(League.enabled).where(League.external_id == 7))
            assert await session.scalar(select(func.count(Odds1X2.id))) == 3
    finally:
        async with db.engine.begin() as conn: await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()
