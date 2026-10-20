"""Relational value parity in disposable loopback PostgreSQL; rollback, no DROP."""
import hashlib
import uuid
from datetime import datetime
from sqlalchemy import insert,DateTime
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from sqlalchemy.schema import CreateSchema
from src.models import Base,League,Season,Team,Referee,Bookmaker,Match,MatchStatistics
from scripts.archive_format import MARKETS,read_archive,encoded
from scripts.archive_readonly_pilot import records


def target_url(url):
    try:
        parsed=make_url(url)
        allowed=parsed.host in ('localhost','127.0.0.1','::1') and parsed.database in ('betapp_test','betapp_archive_restore')
        if parsed.get_backend_name()!='postgresql' or parsed.query or not allowed:raise ValueError()
        return parsed.set(drivername='postgresql+asyncpg')
    except Exception:raise ValueError('Restore requires a separate loopback test PostgreSQL database') from None


def decoded(model,data):
    return {k:datetime.fromisoformat(v) if v is not None and isinstance(model.__table__.c[k].type,DateTime) else v for k,v in data.items()}


async def restore_postgres(archive,manifest,url):
    engine=create_async_engine(target_url(url),connect_args={'command_timeout':15})
    schema='archive_restore_'+uuid.uuid4().hex
    models=[League,Season,Team,Referee,Bookmaker,Match,MatchStatistics,*MARKETS.values()]
    count=quotes=0;cache={}
    try:
        async with engine.connect() as conn:
            tx=await conn.begin()
            try:
                await conn.execute(CreateSchema(schema))
                mapped=await conn.execution_options(schema_translate_map={None:schema})
                await mapped.run_sync(lambda c:Base.metadata.create_all(c,tables=[m.__table__ for m in models]))
                async with AsyncSession(mapped,expire_on_commit=False) as session:
                    async def entity(model,key,data):
                        identity=(model.__tablename__,key)
                        if identity in cache:
                            old_id,old=cache[identity]
                            if old!=data:raise ValueError('Inconsistent linked archive entity')
                            return old_id
                        result=await session.scalar(insert(model).values(**decoded(model,data)).returning(model.id))
                        cache[identity]=(result,data)
                        return result
                    for row in read_archive(archive,manifest):
                        lid=await entity(League,row['league']['external_id'],row['league'])
                        sid=await entity(Season,(lid,row['season']['season_name']),dict(league_id=lid,**row['season']))
                        teams={t['external_id']:await entity(Team,t['external_id'],t) for t in row['teams']}
                        books={b['external_id']:await entity(Bookmaker,b['external_id'],b) for b in row['bookmakers']}
                        ref=row['referee'];rid=await entity(Referee,ref['source_key'],ref) if ref else None
                        match=dict(row['match']);home=match.pop('home_external_id');away=match.pop('away_external_id')
                        mid=await session.scalar(insert(Match).values(**decoded(Match,match),league_id=lid,season_id=sid,
                            home_team_id=teams[home],away_team_id=teams[away],referee_id=rid).returning(Match.id))
                        if row['stats'] is not None:await session.execute(insert(MatchStatistics).values(match_id=mid,**decoded(MatchStatistics,row['stats'])))
                        for quote in row['odds']:
                            q=dict(quote);model=MARKETS[q.pop('market')];book=q.pop('bookmaker_external_id')
                            await session.execute(insert(model).values(match_id=mid,bookmaker_id=books[book],**decoded(model,q)));quotes+=1
                        count+=1
                    await session.flush()
                    digest=hashlib.sha256();restored=0
                    async for row in records(session,manifest['league'],manifest['season']):
                        digest.update((encoded(row)+'\n').encode('utf-8'));restored+=1
                    if (count,quotes,restored)!=(manifest['match_count'],manifest['odds_count'],manifest['match_count']) or digest.hexdigest()!=manifest['payload_sha256']:
                        raise ValueError('PostgreSQL exact value parity mismatch')
                    return dict(backend='postgresql',verified_matches=count,verified_quotes=quotes,sha256=manifest['sha256'],
                        payload_sha256=digest.hexdigest(),exact_value_parity=True,target_rolled_back=True)
            finally:await tx.rollback()
    finally:await engine.dispose()
