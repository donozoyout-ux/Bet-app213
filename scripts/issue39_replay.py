"""Replay captured public records in a disposable PostgreSQL schema, never production."""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from sqlalchemy.schema import CreateSchema, DropSchema
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.db import Database
from src.models import League, Match
from src.jobs.storage import store_match
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.rounds import discover_rounds


async def main(directory, output):
    # Requires an explicit test-only URL, with an isolated schema for all writes.
    db=Database(os.environ['TEST_POSTGRES_URL'])
    schema='issue39_'+uuid.uuid4().hex
    results=[]
    try:
        async with db.engine.begin() as conn:await conn.execute(CreateSchema(schema))
        db.engine=db.engine.execution_options(schema_translate_map={None:schema})
        db.sessions=async_sessionmaker(db.engine,expire_on_commit=False)
        await db.initialize()
        for dbfile in sorted(directory.glob('db-*.json')):
            external=int(dbfile.stem.split('-')[1])
            stored={r['external_match_id']:r for r in json.loads(dbfile.read_text())}
            records={};errors=[]
            for source in sorted(directory.glob(f'source-{external}-*.json')):
                season=source.stem.removeprefix(f'source-{external}-')
                payload=json.loads(source.read_text())
                for number in discover_rounds(payload):
                    for row in parse_matches(payload,number,errors):records[row['external_match_id']]=(season,row)
            async with db.session() as session:
                league=await session.scalar(select(League).where(League.external_id==external))
                for id,(season,row) in records.items():
                    if id not in stored:continue
                    old=stored[id]
                    initial={**row,**{k:old[k] for k in ('status','ht_home','ht_away','ft_home','ft_away')}}
                    initial['kickoff_at']=datetime.fromisoformat(old['kickoff_at'].replace('Z','+00:00')) if old['kickoff_at'] else None
                    await store_match(session,league.id,season,initial)
                await session.commit()
                before=await session.scalar(select(func.count(Match.id)).where(Match.league_id==league.id))
                passes=[]
                for _ in range(2):
                    counters=dict(added=0,updated=0,unchanged=0,discovered=len(records),parse_failed=len(errors))
                    for season,row in records.values():
                        match=await store_match(session,league.id,season,row)
                        counters[match._discovery_change]+=1
                    await session.commit()
                    counters['unique_matches']=await session.scalar(select(func.count(Match.id)).where(Match.league_id==league.id))
                    passes.append(counters)
                results.append(dict(external_id=external,before_unique=before,first=passes[0],repeat=passes[1]))
                print(external,passes,flush=True)
        output.write_text(json.dumps({'basis':'isolated_postgresql_replay_of_captured_public_records',
            'captured_at':datetime.now(timezone.utc).isoformat(),'leagues':results},indent=2))
    finally:
        async with db.engine.begin() as conn:await conn.execute(DropSchema(schema,cascade=True,if_exists=True))
        await db.close()


if __name__=='__main__':asyncio.run(main(Path(sys.argv[1]),Path(sys.argv[2])))
