"""Synthetic fixtures only; no production archive, collector or provider calls."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import httpx
import pytest
import pytest_asyncio
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from src.models import Base, League, Season, Team, Match, PredictionSnapshot, PredictionPublication, BoardEdition
from src.archive.history_format import TABLES, SCHEMA, canonical, sha, schema_digest, seal, unseal, parse, eligibility
from src.archive.history_reader import HistoryReader, HistoryUnavailable, GitHubHistoryObjects

KEY = base64.b64encode(b'x' * 32).decode()
GEN = 'test-source-generation-1'
NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def readers():
    readers = []
    yield readers
    for reader in readers: await reader.close()


class Objects:
    def __init__(self, blobs): self.blobs, self.calls = blobs, []
    async def get(self, meta):
        self.calls.append(meta['id'])
        return self.blobs[meta['id']]
    async def close(self): pass


async def fixture_records(db):
    async with db.session() as session:
        league = await session.scalar(select(League).order_by(League.id))
        season = Season(league_id=league.id, season_name='2025-2026')
        home, away = Team(external_id=999001, name='Fixture A'), Team(external_id=999002, name='Fixture B')
        session.add_all([season, home, away]); await session.flush()
        match = Match(external_match_id=999003, league_id=league.id, season_id=season.id,
            home_team_id=home.id, away_team_id=away.id, round=1,
            kickoff_at=NOW-timedelta(days=2), status='finished', ft_home=0, ft_away=0,
            odds_complete=True, raw={'fixture_only': True, 'multiline': 'a\nb'})
        session.add(match); await session.flush()
        session.add(PredictionSnapshot(match_id=match.id, league_id=league.id,
            captured_at=NOW-timedelta(days=3), picks=[], model_version='fixture-only'))
        session.add(BoardEdition(version='fixture-only', league_id=league.id, created_at=NOW-timedelta(days=3), payload={}))
        session.add(PredictionPublication(match_id=match.id, league_id=league.id,
            published_at=NOW-timedelta(days=3), kickoff_at=match.kickoff_at,
            picks=[], model_version='fixture-only', board_version='fixture-only'))
        await session.commit()
        records = []
        for table in Base.metadata.sorted_tables:
            if table.name not in TABLES: continue
            for row in (await session.execute(select(table))).mappings():
                # SQLite fixture loses timezone; source contract requires UTC.
                data = {k: v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v for k,v in row.items()}
                records.append(dict(table=table.name, row=jsonable_encoder(data)))
        return records, match.id, league.id


def package(records, mid, lid, mutate=None):
    raw = canonical(records)
    encrypted = seal(raw, KEY, GEN, 'chunk-000001.enc')
    catalog = dict(schema=SCHEMA, schema_digest=schema_digest(), generation=GEN,
        source_id='isolated-old-db', created_at=NOW.isoformat(),
        coverage={'from': '2024-01-01T00:00:00+00:00', 'to': NOW.isoformat(), 'league_ids': [lid], 'complete': True},
        counts={name: sum(r['table']==name for r in records) for name in TABLES},
        eligibility={str(mid): dict(settled=True, provider_final=True, odds_final=True, dependencies_complete=True)},
        chunks=[dict(id=2, name='chunk-000001.enc', size=len(encrypted), sha256=sha(encrypted),
                     raw_sha256=sha(raw), raw_bytes=len(raw), rows=len(records))])
    if mutate: mutate(catalog)
    manifest = seal(canonical(catalog), KEY, GEN, 'catalog.enc')
    objects = Objects({1: manifest, 2: encrypted})
    return HistoryReader(objects, key=KEY, generation=GEN, catalog_asset=dict(id=1, size=len(manifest)),
                         catalog_sha256=sha(manifest)), catalog, raw


async def test_exact_restore_query_only_single_flight_and_warm_no_network(db):
    records, mid, lid = await fixture_records(db)
    reader, catalog, raw = package(records, mid, lid)
    try:
        await asyncio.gather(*(reader.prepare() for _ in range(12)))
        assert reader.objects.calls == [1, 2]
        async with reader.session() as session:
            assert (await session.get(Match, mid)).external_match_id == 999003
            assert await session.scalar(text('SELECT raw FROM archive_objects')) == raw
            assert session.info['history_archive']['source_id'] == 'isolated-old-db'
            with pytest.raises(OperationalError):
                await session.execute(text('DELETE FROM prediction_snapshots'))
        async with reader.session() as session:
            assert (await session.get(PredictionSnapshot, mid)).model_version == 'fixture-only'
        assert reader.objects.calls == [1, 2]
    finally: await reader.close()


@pytest.mark.parametrize('fault', ['missing_table', 'count', 'digest', 'missing_dependency',
    'wrong_namespace', 'not_final', 'unverified', 'oversized', 'missing_chunk', 'wrong_key', 'duplicate_row'])
async def test_fail_closed_no_partial_database(db, fault):
    records, mid, lid = await fixture_records(db)
    mutate = None
    if fault == 'missing_dependency': records = [r for r in records if r['table'] != 'teams']
    if fault == 'duplicate_row': records.append(deepcopy(next(r for r in records if r['table']=='matches')))
    if fault == 'not_final': next(r for r in records if r['table']=='matches')['row']['status']='postponed'
    if fault == 'missing_table': mutate = lambda c: c['counts'].pop('match_events')
    if fault == 'count': mutate = lambda c: c['counts'].update(matches=2)
    if fault == 'digest': mutate = lambda c: c['chunks'][0].update(raw_sha256='0'*64)
    if fault == 'wrong_namespace': mutate = lambda c: c.update(generation='another')
    if fault == 'unverified': mutate = lambda c: c['eligibility'][str(mid)].update(provider_final=False)
    if fault == 'oversized': mutate = lambda c: c['chunks'][0].update(raw_bytes=10**12)
    reader, _, _ = package(records, mid, lid, mutate)
    if fault == 'missing_chunk': reader.objects.blobs.pop(2)
    if fault == 'wrong_key': reader.key = base64.b64encode(b'y'*32).decode()
    try:
        with pytest.raises(HistoryUnavailable): await reader.prepare()
        assert reader.engine is reader.catalog is reader.directory is None
        calls = len(reader.objects.calls)
        with pytest.raises(HistoryUnavailable): await reader.prepare()
        assert len(reader.objects.calls) == calls  # failure cooldown
    finally: await reader.close()


@pytest.mark.parametrize('status', ['scheduled', 'live', 'postponed', 'cancelled', 'unknown'])
def test_24_hours_alone_never_qualifies(status):
    row = dict(kickoff_at=(NOW-timedelta(days=10)).isoformat(), status=status, ft_home=0, ft_away=0)
    evidence = dict(settled=True, provider_final=True, odds_final=True, dependencies_complete=True)
    assert 'not_final' in eligibility(row, evidence, NOW)
    row['status'] = 'finished'
    assert not eligibility(row, evidence, NOW)
    row['kickoff_at'] = (NOW-timedelta(hours=23)).isoformat()
    assert 'too_recent' in eligibility(row, evidence, NOW)


async def test_existing_http_routes_use_same_sql_without_touching_hot_database(db, monkeypatch, readers):
    from src.api.main import app
    from src.api import routes
    import src.api.history_read as service
    records, mid, lid = await fixture_records(db)
    reader, _, _ = package(records, mid, lid)
    readers.append(reader)
    monkeypatch.setattr(service, 'configured_reader', lambda: reader)
    monkeypatch.setattr(routes, 'database', db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        suffix = f'?history_source=archive&history_generation={GEN}'
        for path in [f'/api/matches/{mid}', f'/api/matches/{mid}/odds',
                     f'/api/matches/{mid}/prediction-audit', '/api/market-performance',
                     '/api/history/seasons', '/api/learning/lessons', '/api/learning/reports',
                     '/api/performance-trends', '/api/chart-diagnostics']:
            hot = await client.get(path)
            cold = await client.get(path+suffix)
            assert hot.status_code == cold.status_code == 200, (path, cold.text)
            # Coverage CSV contains cache scope/time metadata; compare counts below.
            if path != '/api/history/seasons':
                a, b = hot.json(), cold.json()
                if isinstance(a, dict):
                    a.pop('checked_at', None); b.pop('checked_at', None)
                assert a == b, path
            assert cold.headers['x-history-generation'] == GEN
        for tail in ('dossier', 'history'):
            response = await client.get(f'/api/matches/{mid}/{tail}'+suffix)
            assert response.status_code == 200, response.text
            if tail == 'history':
                assert isinstance(response.json()['coverage'], str)
                assert response.json()['archive_coverage']['generation'] == GEN
        # An overlapping new DB integer ID must NEVER override this source.
        async with db.session() as session:
            match = await session.get(Match, mid)
            match.external_match_id = 888888
            await session.commit()
        db.ready = False
        assert (await client.get(f'/api/matches/{mid}')).status_code == 503
        cold = await client.get(f'/api/matches/{mid}'+suffix)
        assert cold.status_code == 200
        assert cold.json()['external_match_id'] == 999003
        assert (await client.get(f'/api/matches/{mid}?history_source=archive')).status_code == 409
        assert (await client.post('/api/coupon/quote'+suffix, json={'items': []})).status_code == 400
        assert len(reader.objects.calls) == 2
    await reader.close()


async def test_authenticated_private_get_only_and_rate_limit(db):
    records, mid, lid = await fixture_records(db)
    reader, _, _ = package(records, mid, lid)
    blobs = reader.objects.blobs
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.headers['authorization'] == 'Bearer fixture-token'
        if '/assets/' in request.url.path:
            return httpx.Response(200, content=blobs[int(request.url.path.rsplit('/',1)[1])])
        return httpx.Response(200, json={'private': True, 'full_name': 'donozoyout-ux/kuponanalizii-data-archive'})
    reader.objects = GitHubHistoryObjects('donozoyout-ux/kuponanalizii-data-archive', 'fixture-token', KEY,
                                         transport=httpx.MockTransport(handler))
    try:
        await reader.prepare()
        await reader.prepare()
        assert len(calls) == 3
    finally: await reader.close()
    remote = GitHubHistoryObjects('a/b', 'fixture-token', KEY,
        transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    try:
        with pytest.raises(HistoryUnavailable): await remote.get(dict(id=1,size=100))
        assert remote.blocked_until > 0
    finally: await remote.close()


def test_authenticated_context_and_ambiguous_json_rejected():
    encrypted = seal(b'[]', KEY, GEN, 'chunk-000001.enc')
    for generation, name in [('another', 'chunk-000001.enc'), (GEN, 'catalog.enc')]:
        with pytest.raises(Exception): unseal(encrypted, KEY, generation, name)
    for raw in (b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}'):
        with pytest.raises(ValueError): parse(raw)


@pytest.mark.parametrize('table', ['board_editions', 'prediction_settlements'])
async def test_missing_embedded_dependencies_rejected(db, table):
    records, mid, lid = await fixture_records(db)
    if table == 'board_editions':
        records = [r for r in records if r['table'] != table]
    else:
        # A recorded pick without a terminal settlement is not eligible.
        next(r for r in records if r['table']=='prediction_snapshots')['row']['picks'] = [{'fixture_only':True}]
    reader, _, _ = package(records, mid, lid)
    try:
        with pytest.raises(HistoryUnavailable): await reader.prepare()
    finally: await reader.close()


async def test_restore_disk_budget_and_cli_no_overwrite(db, monkeypatch, tmp_path):
    import src.archive.history_restore as restore_module
    import src.archive.history_cli as cli
    records, mid, lid = await fixture_records(db)
    reader, _, _ = package(records, mid, lid)
    monkeypatch.setattr(cli, 'configured_reader', lambda: reader)
    monkeypatch.setattr(cli, 'close_reader', reader.close)
    output = tmp_path/'isolated.sqlite'
    result = await cli.run(output)
    assert result['status'] == 'verified' and result['full_database_backup'] is False
    assert result['source_space_reclaimed'] == 0
    original = output.read_bytes()
    with pytest.raises(FileExistsError): await cli.run(output)
    assert output.read_bytes() == original
    reader, _, _ = package(records, mid, lid)
    monkeypatch.setattr(restore_module, 'MAX_RESTORE_BYTES', 1)
    try:
        with pytest.raises(HistoryUnavailable): await reader.prepare()
        assert reader.engine is None
    finally: await reader.close()


async def test_public_repository_never_downloads_archive():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'private':False, 'full_name':'a/b'})
    remote = GitHubHistoryObjects('a/b', 'fixture-token', KEY, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(HistoryUnavailable): await remote.get(dict(id=1,size=100))
        assert len(calls) == 1 and '/assets/' not in calls[0].url.path
    finally: await remote.close()
