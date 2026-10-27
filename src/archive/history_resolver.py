"""Shared site/model history, provider identity deduplication, hot precedence."""
from copy import copy
from datetime import timedelta
import os
from types import SimpleNamespace
from sqlalchemy import select, or_
from sqlalchemy.orm import aliased, defer
from src.models import Match, MatchStatistics, League, Team, Season, Referee
from src.jobs.worker import aware
from .history_index import configured_index
from .history_reader import HistoryUnavailable


def enabled():return os.getenv('HISTORY_RESOLVER_ENABLED')=='true'


async def engine_rows(session,league_id,cutoff,exclude_id):
    key=(league_id,cutoff,exclude_id)
    cache=session.info.setdefault('archive_engine_rows',{})
    if key in cache:return cache[key]
    league=await session.get(League,league_id)
    if league is None:return []
    home,away=aliased(Team),aliased(Team)
    query=select(Match,MatchStatistics,home.name,away.name).join(home,Match.home_team_id==home.id).join(away,Match.away_team_id==away.id).outerjoin(MatchStatistics).where(Match.league_id==league_id)
    hot=list((await session.execute(query.options(defer(Match.raw),defer(MatchStatistics.raw)).limit(2001))).all())
    if len(hot)>2000:raise HistoryUnavailable('Hot history query budget')
    hot_ids={row[0].external_match_id for row in hot}
    team_rows=list(await session.scalars(select(Team).limit(4001)))
    if len(team_rows)>4000:raise HistoryUnavailable('Team identity budget')
    teams={r.external_id:r.id for r in team_rows}
    seasons={r.season_name:r.id for r in await session.scalars(select(Season).where(Season.league_id==league_id))}
    referees={r.source_key:r.id for r in await session.scalars(select(Referee).limit(4001))}
    if len(referees)>4000:raise HistoryUnavailable('Referee identity budget')
    rows=list(hot);seen=set(hot_ids)
    index=configured_index()
    for part,identities in await index.selected(league=league.external_id,cutoff=cutoff):
        ids=[m['id'] for m in identities if m['provider']==league.source and m['external_match_id'] not in seen]
        if not ids:continue
        async with index.session(part['generation']) as (cold,_):
            hc,ac=aliased(Team),aliased(Team)
            query=select(Match,MatchStatistics,hc.external_id,ac.external_id,hc.name,ac.name,Season.season_name,Referee.source_key).join(hc,Match.home_team_id==hc.id).join(ac,Match.away_team_id==ac.id).join(Season,Match.season_id==Season.id).outerjoin(Referee,Match.referee_id==Referee.id).outerjoin(MatchStatistics).where(Match.id.in_(ids))
            for m,s,he,ae,hn,an,season,referee in (await cold.execute(query.options(defer(Match.raw),defer(MatchStatistics.raw)))).all():
                if m.external_match_id in seen:continue
                seen.add(m.external_match_id)
                values={col.name:getattr(m,col.name) for col in Match.__table__.c if col.name!='raw'}
                values.update(id=-m.external_match_id,league_id=league_id,home_team_id=teams.get(he,-he),away_team_id=teams.get(ae,-ae),
                    season_id=seasons.get(season,-m.season_id),referee_id=referees.get(referee,-m.referee_id if m.referee_id else None))
                stats=SimpleNamespace(**{c.name:getattr(s,c.name) for c in MatchStatistics.__table__.c if c.name!='raw'}) if s else None
                rows.append((SimpleNamespace(**values),stats,hn,an))
    if len(rows)>2000:raise HistoryUnavailable('Combined history query budget')
    result=[]
    for m,s,h,a in rows:
        if m.id==exclude_id or m.status!='finished' or not m.kickoff_at:continue
        if not cutoff-timedelta(days=3*366)<=aware(m.kickoff_at)<=cutoff-timedelta(hours=3):continue
        if any(aware(v)>cutoff for v in (m.created_at,m.updated_at,m.last_scraped_at) if v):continue
        if s and (not s.is_final or aware(s.updated_at)>cutoff):s=None
        result.append((m,s,h,a))
    result.sort(key=lambda r:(aware(r[0].kickoff_at),r[0].id),reverse=True)
    cache[key]=result
    return result


async def site_history(session,match,scope,limit,offset,cutoff):
    from src.api.routes import match_query,match_response
    from src.api.match_page import observations
    selected=match.home_team_id if scope.startswith('home') else match.away_team_id
    target_ids={match.home_team_id,match.away_team_id} if scope=='h2h' else {selected}
    team_rows=list(await session.scalars(select(Team).where(Team.id.in_(target_ids))))
    external={t.external_id for t in team_rows};local={t.external_id:t.id for t in team_rows}
    q,_,_=match_query()
    hot=(await session.execute(q.options(defer(Match.raw)).where(or_(Match.home_team_id.in_(target_ids),Match.away_team_id.in_(target_ids))).limit(2001))).all()
    if len(hot)>2000:raise HistoryUnavailable('Hot team history budget')
    hot_external={row[0].external_match_id for row in hot};seen=set(hot_external)
    stats={s.match_id:s for s in await session.scalars(select(MatchStatistics).where(MatchStatistics.match_id.in_([r[0].id for r in hot])))}
    values=[dict(**match_response(r),home_team_id=r[0].home_team_id,away_team_id=r[0].away_team_id,statistics=observations(stats.get(r[0].id))) for r in hot]
    index=configured_index()
    league=await session.get(League,match.league_id)
    for part,identities in await index.selected(teams=external,cutoff=cutoff):
        wanted={m['id']:m for m in identities if m['provider']==league.source and m['external_match_id'] not in seen}
        if not wanted:continue
        async with index.session(part['generation']) as (cold,_):
            q,_,_=match_query()
            records=(await cold.execute(q.options(defer(Match.raw)).where(Match.id.in_(wanted)))).all()
            stats={s.match_id:s for s in await cold.scalars(select(MatchStatistics).where(MatchStatistics.match_id.in_(wanted)))}
            for r in records:
                m=r[0];identity=wanted[m.id]
                if m.external_match_id in seen:continue
                seen.add(m.external_match_id)
                values.append(dict(**match_response(r),home_team_id=local.get(identity['home'],-identity['home']),
                    away_team_id=local.get(identity['away'],-identity['away']),statistics=observations(stats.get(m.id)),
                    archive_generation=part['generation'],archive_identity=identity['identity']))
    if len(values)>2000:raise HistoryUnavailable('Combined team history budget')
    def keep(m):
        if m['status']!='finished' or not m['kickoff_at'] or aware(__import__('datetime').datetime.fromisoformat(m['kickoff_at']) if isinstance(m['kickoff_at'],str) else m['kickoff_at'])>=cutoff:return False
        h,a=m['home_team_id'],m['away_team_id']
        if scope=='h2h':return {h,a}=={match.home_team_id,match.away_team_id}
        if scope=='home_venue':return h==selected
        if scope=='away_venue':return a==selected
        return selected in (h,a)
    values=[m for m in values if keep(m)]
    values.sort(key=lambda m:(str(m['kickoff_at']),m.get('archive_identity',''),m['id']),reverse=True)
    return dict(items=values[offset:offset+limit],has_more=len(values)>offset+limit,offset=offset,limit=limit,cutoff=cutoff,
        coverage='Aktif kayıtlar ve doğrulanmış arşiv; aynı sağlayıcı maçında aktif kayıt önceliklidir.',
        archive_coverage={'run_id':index.run_id,'retained':len(index.data['coverage']['retained'])})
