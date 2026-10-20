"""Bounded consistent READ ONLY export; never initializes source schema."""
import argparse
import asyncio
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, text
from sqlalchemy.orm import aliased
from src.db import database
from src.models import League, Season, Team, Referee, Match, MatchStatistics, Bookmaker
from scripts.archive_format import (BOOKS,MARKETS,LEAGUE_FIELDS,TEAM_FIELDS,REFEREE_FIELDS,
    MATCH_FIELDS,STAT_FIELDS,QUOTE_FIELDS,Writer,encoded,normalized,partition,read_archive,write_archive)


def values(model,names):
    return {name:getattr(model,name) for name in names}


async def records(session,league_id,season_name,batch_size=50):
    """Keyset pages and bulk child reads, inside caller's consistent snapshot."""
    if not 1<=batch_size<=100:raise ValueError('Archive batch size must be 1-100')
    home,away=aliased(Team),aliased(Team)
    last=0
    books=(await session.scalars(select(Bookmaker).where(Bookmaker.external_id.in_(BOOKS)).order_by(Bookmaker.external_id))).all()
    if {b.external_id:b.name for b in books}!=BOOKS:raise ValueError('Source bookmakers are incomplete or mismatched')
    book_ids={b.id:b.external_id for b in books}
    while True:
        page=(await session.execute(select(Match,League,Season,home,away)
            .join(League,Match.league_id==League.id).join(Season,Match.season_id==Season.id)
            .join(home,Match.home_team_id==home.id).join(away,Match.away_team_id==away.id)
            .where(League.external_id==league_id,Season.season_name==season_name,
                Match.status=='finished',Match.kickoff_at>=datetime(2024,1,1,tzinfo=timezone.utc),
                Match.external_match_id>last).order_by(Match.external_match_id).limit(batch_size))).all()
        if not page:break
        ids=[m.id for m,*_ in page]
        stats={s.match_id:s for s in (await session.scalars(select(MatchStatistics).where(MatchStatistics.match_id.in_(ids)))).all()}
        refs={r.id:r for r in (await session.scalars(select(Referee).where(Referee.id.in_(
            [m.referee_id for m,*_ in page if m.referee_id is not None])))).all()}
        quotes={i:[] for i in ids}
        for market,model in MARKETS.items():
            for q in (await session.scalars(select(model).where(model.match_id.in_(ids),model.bookmaker_id.in_(book_ids))
                    .order_by(model.match_id,model.bookmaker_id))).all():
                quotes[q.match_id].append(dict(market=market,bookmaker_external_id=book_ids[q.bookmaker_id],**values(q,QUOTE_FIELDS[market])))
        for match,league,season,h,a in page:
            if season.league_id!=league.id or (match.referee_id is not None and match.referee_id not in refs):
                raise ValueError('Source linked identity mismatch')
            row=dict(league=values(league,LEAGUE_FIELDS),season={'season_name':season.season_name},
                teams=[values(t,TEAM_FIELDS) for t in sorted((h,a),key=lambda t:t.external_id)],
                referee=values(refs[match.referee_id],REFEREE_FIELDS) if match.referee_id else None,
                match=dict(**values(match,MATCH_FIELDS),home_external_id=h.external_id,away_external_id=a.external_id),
                stats=values(stats[match.id],STAT_FIELDS) if match.id in stats else None,
                bookmakers=[{'external_id':b.external_id,'name':b.name} for b in books],
                odds=sorted(quotes[match.id],key=lambda q:(q['market'],q['bookmaker_external_id'])))
            yield json.loads(encoded(row))
            last=match.external_match_id
        session.expunge_all()


async def iter_records(league_id,season_name,db=None,batch_size=50):
    partition(league_id,season_name)
    db=db or database
    async with db.session() as session:
        if session.bind.dialect.name!='postgresql':raise RuntimeError('Archive source must be PostgreSQL')
        await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        await session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        await session.execute(text("SET LOCAL idle_in_transaction_session_timeout = '15000ms'"))
        if await session.scalar(text('SHOW transaction_read_only'))!='on':raise RuntimeError('Source read-only transaction required')
        async with asyncio.timeout(120):
            async for row in records(session,league_id,season_name,batch_size):yield row


async def collect(league_id,season_name,db=None):
    """Bounded compatibility API; production uses export_archive streaming."""
    from scripts.archive_format import MAX_PAYLOAD,MAX_ROWS
    result=[];size=0
    async for row in iter_records(league_id,season_name,db):
        size+=len(encoded(row).encode('utf-8'))
        if size>MAX_PAYLOAD or len(result)>=MAX_ROWS:raise ValueError('Archive collection limit exceeded')
        result.append(row)
    return result


async def export_archive(league_id,season_name,output,db=None):
    with Writer(output) as writer:
        async for row in iter_records(league_id,season_name,db):writer.add(row)
        return writer.finish()


def restore_verify(archive,manifest,db_path):
    """SQLite logical check only; never presented as PostgreSQL proof."""
    if Path(db_path).exists():raise ValueError('Fresh SQLite target required')
    conn=sqlite3.connect(str(db_path));digest=hashlib.sha256();count=quotes=0;success=False
    try:
        conn.execute('CREATE TABLE fixtures (match_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE quotes (match_id INTEGER, market TEXT, book INTEGER, payload TEXT, PRIMARY KEY(match_id,market,book), FOREIGN KEY(match_id) REFERENCES fixtures(match_id))')
        conn.execute('PRAGMA foreign_keys=ON')
        for row in read_archive(archive,manifest):
            mid=row['match']['external_match_id']
            conn.execute('INSERT INTO fixtures VALUES (?,?)',(mid,encoded(row)))
            for q in row['odds']:
                conn.execute('INSERT INTO quotes VALUES (?,?,?,?)',(mid,q['market'],q['bookmaker_external_id'],encoded(q)))
                quotes+=1
            count+=1
        for payload, in conn.execute('SELECT payload FROM fixtures ORDER BY match_id'):digest.update((payload+'\n').encode('utf-8'))
        if digest.hexdigest()!=manifest['payload_sha256']:raise ValueError('SQLite value parity mismatch')
        conn.commit();success=True
        return dict(verified_matches=count,verified_quotes=quotes,sha256=manifest['sha256'],backend='sqlite-logical')
    finally:
        conn.close()
        if not success:Path(db_path).unlink(missing_ok=True)


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--league',type=int,required=True,choices=(6,7));parser.add_argument('--season',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        manifest=await export_archive(args.league,args.season,args.output)
        with tempfile.TemporaryDirectory() as temp:proof=restore_verify(args.output,manifest,Path(temp)/'restore.sqlite')
        print(json.dumps(dict(match_count=manifest['match_count'],odds_count=manifest['odds_count'],sha256=manifest['sha256'],sqlite_restore=proof)))
    finally:await database.close()


if __name__=='__main__':asyncio.run(main())
