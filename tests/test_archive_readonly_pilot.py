"""Synthetic fixtures stay in test PostgreSQL; no production access."""
import gzip
import hashlib
import json
import os
import sqlite3
import uuid
from copy import deepcopy
from pathlib import Path
import pytest
from sqlalchemy import select,insert,text,event
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema
from src.db import Database
from src.models import Base,League,Season,Team,Referee,Match,MatchStatistics,Bookmaker
from scripts.archive_format import *
from scripts.archive_readonly_pilot import collect,iter_records,restore_verify
from scripts.archive_postgres_restore import restore_postgres,decoded,target_url

STAMP='2025-01-01T10:00:00+00:00'


def sample():
    league=dict.fromkeys(LEAGUE_FIELDS)
    league.update(external_id=7,name='Denmark',country='Denmark',source='goaloo',competition_type='club',
        enabled=False,backfill_from_year=2024,priority=100,schedule_format='league',latest_season='2025-2026',created_at=STAMP,updated_at=STAMP)
    match=dict.fromkeys(MATCH_FIELDS)
    match.update(external_match_id=202411,round=1,kickoff_at=STAMP,status='finished',ft_home=1,ft_away=0,
        ht_home=None,ht_away=0,raw={'vendor':None},odds_complete=False,created_at=STAMP,updated_at=STAMP,
        home_external_id=11,away_external_id=12)
    stats=dict.fromkeys(STAT_FIELDS)
    stats.update(home_corners=0,is_final=True,collection_status='partial',source='goaloo',
        raw={'observed':None,'unicode':'København'},updated_at=STAMP)
    quotes=[]
    for market,fields in QUOTE_FIELDS.items():
        for book in BOOKS:
            q=dict.fromkeys(fields)
            q.update(market=market,bookmaker_external_id=book,raw={'_field_observed_at':{'opening_home':STAMP},'_field_lines':{'opening_home':0},'null':None},updated_at=STAMP)
            for name in fields:
                if name.startswith(('opening_','closing_')):q[name]=0 if name.endswith('_line') else 1.9
            if market=='1x2' and book==3:q['raw']=None
            if market=='ou':q['closing_under']=None
            quotes.append(q)
    return [dict(league=league,season={'season_name':'2025-2026'},teams=[dict(external_id=11,name='København'),dict(external_id=12,name='Away')],
        referee=dict(source_key='goaloo:1',external_id=None,name='Referee',source='goaloo'),match=match,stats=stats,
        bookmakers=[dict(external_id=i,name=n) for i,n in BOOKS.items()],odds=sorted(quotes,key=lambda q:(q['market'],q['bookmaker_external_id'])))]


def test_isolated_sqlite_restore_proves_logical_parity(tmp_path):
    archive=tmp_path/'season.gz';manifest=write_archive(sample(),archive)
    proof=restore_verify(archive,manifest,tmp_path/'restore.sqlite')
    assert proof['verified_matches']==1 and proof['verified_quotes']==9 and proof['backend']=='sqlite-logical'
    with sqlite3.connect(tmp_path/'restore.sqlite') as conn:
        assert conn.execute('SELECT count(*) FROM fixtures').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM quotes').fetchone()[0]==9


def test_deterministic_bytes_and_null_roundtrip(tmp_path):
    a,b=tmp_path/'a.gz',tmp_path/'b.gz'
    one,two=write_archive(sample(),a),write_archive(sample(),b)
    assert one['sha256']==two['sha256'] and one['payload_sha256']==two['payload_sha256']
    row=list(read_archive(a,one))[0]
    assert row==sample()[0] and row['match']['ht_home'] is None
    assert row['odds'][0]['raw'] is None and row['stats']['home_corners']==0


def test_corruption_rejected(tmp_path):
    a=tmp_path/'a.gz';m=write_archive(sample(),a);a.write_bytes(a.read_bytes()+b'tamper')
    with pytest.raises(ValueError,match='checksum'):list(read_archive(a,m))


@pytest.mark.parametrize('kind',['duplicates','missing_team','unknown_book','duplicate_quote','missing_column','bad_line','nan','wrong_team'])
def test_bad_records_fail_without_partial_pair(tmp_path,kind):
    rows=sample();r=rows[0]
    if kind=='duplicates':rows*=2
    if kind=='missing_team':r['teams'].pop()
    if kind=='unknown_book':r['odds'][0]['bookmaker_external_id']=999
    if kind=='duplicate_quote':r['odds'][1]=deepcopy(r['odds'][0])
    if kind=='missing_column':del r['match']['raw']
    if kind=='bad_line':r['odds'][-1]['opening_line']='not a line'
    if kind=='nan':r['odds'][0]['opening_home']=float('nan')
    if kind=='wrong_team':r['match']['home_external_id']=999
    a=tmp_path/'bad.gz'
    with pytest.raises(ValueError):write_archive(rows,a)
    assert not a.exists() and not a.with_suffix('.gz.manifest.json').exists()
    assert not list(tmp_path.glob('.archive-*'))


@pytest.mark.parametrize('field,value',[('odds_count',8),('match_count',2),('sha256','bad'),('schema_version',99),('season','2024-2025'),('contains_personal_account_data',True)])
def test_bad_manifest_fails(tmp_path,field,value):
    a=tmp_path/'a.gz';m=write_archive(sample(),a);m[field]=value
    with pytest.raises(ValueError):list(read_archive(a,m))


def test_streaming_cap_and_generator_failure_clean_up(tmp_path,monkeypatch):
    import scripts.archive_format as fmt
    monkeypatch.setattr(fmt,'MAX_LINE',10)
    with pytest.raises(ValueError,match='limit'):write_archive(sample(),tmp_path/'bounded.gz')
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(fmt,'MAX_LINE',2*1024*1024)
    def interrupted():
        yield sample()[0]
        raise RuntimeError('source interrupted')
    with pytest.raises(RuntimeError):write_archive(interrupted(),tmp_path/'partial.gz')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url',['postgresql://u:secret@production/betappv33','postgresql://u:secret@localhost/betappv33','sqlite:///x','postgresql://u:secret@localhost/betapp_test?options=bad'])
def test_source_can_never_be_restore_target(url):
    with pytest.raises(ValueError,match='loopback') as err:target_url(url)
    assert 'secret' not in str(err.value)


def test_private_publication_gates():
    from scripts.archive_release_gate import validate_destination
    good=dict(full_name='donozoyout-ux/ma-larii-archive',private=True,visibility='private')
    validate_destination(good,'a'*40)
    for bad in ({**good,'private':False},{**good,'visibility':'public'},
                {**good,'full_name':'donozoyout-ux/Bet-app213'}):
        with pytest.raises(ValueError):validate_destination(bad,'a'*40)
    with pytest.raises(ValueError):validate_destination(good,'main')
    with pytest.raises(ValueError,match='disabled'):validate_destination(good,'a'*40,True)


@pytest.fixture
async def pg():
    if not os.getenv('TEST_POSTGRES_URL'):pytest.skip('Isolated PostgreSQL is provided by CI')
    target_url(os.environ['TEST_POSTGRES_URL'])
    db=Database(os.environ['TEST_POSTGRES_URL']);schema='archive_source_'+uuid.uuid4().hex
    async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
    db.engine=db.engine.execution_options(schema_translate_map={None:schema})
    db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
    models=[League,Season,Team,Referee,Bookmaker,Match,MatchStatistics,*MARKETS.values()]
    async with db.engine.begin() as conn:await conn.run_sync(lambda c:Base.metadata.create_all(c,tables=[m.__table__ for m in models]))
    r=sample()[0]
    async with db.session() as s:
        lid=await s.scalar(insert(League).values(**decoded(League,r['league'])).returning(League.id))
        sid=await s.scalar(insert(Season).values(league_id=lid,**r['season']).returning(Season.id))
        teams={t['external_id']:await s.scalar(insert(Team).values(**t).returning(Team.id)) for t in r['teams']}
        books={b['external_id']:await s.scalar(insert(Bookmaker).values(**b).returning(Bookmaker.id)) for b in r['bookmakers']}
        rid=await s.scalar(insert(Referee).values(**r['referee']).returning(Referee.id))
        for i in range(3):
            m=dict(r['match']);m.pop('home_external_id');m.pop('away_external_id');m['external_match_id']+=i
            mid=await s.scalar(insert(Match).values(**decoded(Match,m),league_id=lid,season_id=sid,home_team_id=teams[11],away_team_id=teams[12],referee_id=rid).returning(Match.id))
            await s.execute(insert(MatchStatistics).values(match_id=mid,**decoded(MatchStatistics,r['stats'])))
            for q in r['odds']:
                d=dict(q);model=MARKETS[d.pop('market')];book=d.pop('bookmaker_external_id')
                await s.execute(insert(model).values(match_id=mid,bookmaker_id=books[book],**decoded(model,d)))
        await s.commit()
    try:yield db
    finally:await db.close()  # Ephemeral CI database disposal; no destructive SQL.


@pytest.mark.asyncio
async def test_postgres_exact_restore_and_source_select_only(pg,tmp_path):
    statements=[]
    def capture(conn,cursor,statement,*args):statements.append(statement.lstrip().split()[0].upper())
    event.listen(pg.engine.sync_engine,'before_cursor_execute',capture)
    first=await collect(7,'2025-2026',pg);second=await collect(7,'2025-2026',pg)
    event.remove(pg.engine.sync_engine,'before_cursor_execute',capture)
    assert first==second and len(first)==3 and set(statements)<={'SET','SHOW','SELECT'}
    a,b=tmp_path/'a.gz',tmp_path/'b.gz';m=write_archive(first,a);n=write_archive(second,b)
    assert m['sha256']==n['sha256'] and m['odds_count']==27
    proof=await restore_postgres(a,m,os.environ['TEST_POSTGRES_URL'])
    assert proof['backend']=='postgresql' and proof['exact_value_parity'] and proof['target_rolled_back']
    assert await collect(7,'2025-2026',pg)==first
    async with pg.session() as s:
        assert await s.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'archive_restore_%'"))==0


@pytest.mark.asyncio
async def test_repeatable_read_under_concurrent_synthetic_update(pg):
    stream=iter_records(7,'2025-2026',pg,batch_size=1)
    first=await anext(stream)
    async with pg.session() as s:
        await s.execute(Match.__table__.update().where(Match.external_match_id==202412).values(ft_home=9));await s.commit()
    rest=[row async for row in stream]
    assert first['match']['ft_home']==1 and rest[0]['match']['ft_home']==1
    assert (await collect(7,'2025-2026',pg))[1]['match']['ft_home']==9


@pytest.mark.asyncio
async def test_postgres_rejects_valid_hash_but_inconsistent_entity(pg,tmp_path):
    rows=await collect(7,'2025-2026',pg)
    rows[1]['teams'][0]['name']='Changed identity'
    a=tmp_path/'a.gz';m=write_archive(rows,a)
    with pytest.raises(ValueError,match='linked'):await restore_postgres(a,m,os.environ['TEST_POSTGRES_URL'])
    async with pg.session() as s:
        assert await s.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'archive_restore_%'"))==0
