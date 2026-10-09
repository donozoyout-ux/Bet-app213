"""Forward-only, immutable prematch picks; settlement uses published final data."""
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.orm import defer
from src.models import PredictionSnapshot, Match, MatchStatistics, League
from src.jobs.worker import aware
from src.analytics.recommendations import market_matches


async def capture(session, match, picks, now):
    if match.status != 'scheduled' or not match.kickoff_at or aware(match.kickoff_at) <= now or not picks:return False
    known=session.info.get('captured_matches')
    if (match.id in known) if known is not None else (await session.get(PredictionSnapshot,match.id)):
        return False
    if session.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    result=await session.execute(insert(PredictionSnapshot).values(match_id=match.id,league_id=match.league_id,captured_at=now,model_version='quality-poisson-v2-cards',picks=picks).on_conflict_do_nothing(index_elements=['match_id']))
    if known is not None:known.add(match.id)
    return result.rowcount > 0


async def capture_many(session, observations, now):
    """Publish a board atomically without an INSERT round trip per match."""
    known = session.info.get('captured_matches', set())
    records = [dict(match_id=m.id, league_id=m.league_id, captured_at=now,
                    model_version='quality-poisson-v2-cards', picks=picks)
               for m,picks in observations if m.id not in known and picks and
               m.status=='scheduled' and m.kickoff_at and aware(m.kickoff_at)>now]
    if not records:
        return False
    if session.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    result = await session.execute(insert(PredictionSnapshot).values(records).on_conflict_do_nothing(index_elements=['match_id']))
    return result.rowcount > 0


def settle(match, stats, pick):
    if match.status in {'cancelled','abandoned','postponed'}:return 'void',None
    if match.status != 'finished':return 'pending',None
    market,selection=pick['market'],pick['selection']
    if market in {'corners','cards','yellow_cards','red_cards'}:
        fields=['home_corners','away_corners'] if market=='corners' else ['home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards']
        if market in {'yellow_cards','red_cards'}:fields=['home_'+market,'away_'+market]
        if not stats or not stats.is_final or any(getattr(stats,f) is None for f in fields):return 'awaiting_data',None
        total=sum(getattr(stats,f) for f in fields)
    else:
        if match.ft_home is None or match.ft_away is None:return 'awaiting_data',None
        home,away=match.ft_home,match.ft_away;total=home+away
    if market in {'goals','corners','cards','yellow_cards','red_cards'}:
        direction,a,b=selection.split('_');line=float(a+'.'+b);won=total>line if direction=='over' else total<line;actual=total
    elif market=='btts':won=(home>0 and away>0)==(selection=='yes');actual=f'{home}-{away}'
    elif market=='result':won={'home':home>away,'draw':home==away,'away':home<away}[selection];actual=f'{home}-{away}'
    elif market=='double_chance':won={'1x':home>=away,'x2':home<=away,'12':home!=away}[selection];actual=f'{home}-{away}'
    elif market=='asian_handicap':
        side,sign,a,b=selection.split('_');line=float(a+'.'+b)*(1 if sign=='plus' else -1);difference=home-away if side=='home' else away-home
        if difference+line==0:return 'void',f'{home}-{away}'
        won=difference+line>0;actual=f'{home}-{away}'
    else:return 'awaiting_data',None
    return ('won' if won else 'lost'),actual


def tally():return dict(total=0,settled=0,won=0,lost=0,pending=0,awaiting_data=0,void=0,reliability_sum=0,sample_sum=0,last_10=[])


def add(result,pick,outcome):
    result['total']+=1;result['reliability_sum']+=pick['reliability'];result['sample_sum']+=pick['sample_size']
    result[outcome]+=1
    if outcome in {'won','lost'}:result['settled']+=1
    if len(result['last_10'])<10:result['last_10'].append(outcome)


def finish(result):
    result=dict(result);n=result['total'];settled=result['settled']
    result['success_rate']=result['won']/settled if settled else None
    result['average_reliability']=result.pop('reliability_sum')/n if n else None
    result['average_sample_size']=result.pop('sample_sum')/n if n else None
    return result


async def performance(session, league=None, start=None, end=None, market='all', exact_market=None, confidence=None, limit=50, offset=0):
    from src.api.routes import match_query,match_response
    query,_,_=match_query()
    query=query.add_columns(PredictionSnapshot,MatchStatistics).options(defer(Match.raw),defer(MatchStatistics.raw)).join(PredictionSnapshot,PredictionSnapshot.match_id==Match.id).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id).where(League.enabled.is_(True))
    if league:query=query.where(League.id==int(league)) if league.isdigit() else query.where(League.name.ilike('%'+league+'%'))
    if start:query=query.where(PredictionSnapshot.captured_at>=start)
    if end:query=query.where(PredictionSnapshot.captured_at<end)
    query=query.order_by(PredictionSnapshot.captured_at.desc(),Match.id.desc()).execution_options(yield_per=200)
    summary=tally();families=defaultdict(tally);exact=defaultdict(tally);labels={};items=[];count=0
    async for row in await session.stream(query):
        match=row[0];snapshot,stats=row[-2:]
        # Never accept a record captured at/after kickoff, including imported bad rows.
        if not match.kickoff_at or aware(snapshot.captured_at)>=aware(match.kickoff_at):continue
        for pick in snapshot.picks:
            if not market_matches([pick],market) or exact_market and pick['id']!=exact_market or confidence and pick['confidence']!=confidence:continue
            outcome,actual=settle(match,stats,pick)
            add(summary,pick,outcome);add(families[pick['market']],pick,outcome);add(exact[pick['id']],pick,outcome);labels[pick['id']]=(pick['market'],pick['label'])
            if offset<=count<offset+limit:items.append(dict(match=match_response(row[:5]),recommendation=pick,captured_at=aware(snapshot.captured_at),model_version=snapshot.model_version,outcome=outcome,actual=actual))
            count+=1
    from src.diagnostics import statistics_coverage
    coverage=await statistics_coverage(session)
    return dict(stats_coverage_by_league=coverage,availability='saved_prematch_picks' if count else 'no_saved_prematch_picks',summary=finish(summary),families=[dict(market=key,**finish(value)) for key,value in families.items()],markets=[dict(id=key,market=labels[key][0],label=labels[key][1],**finish(value)) for key,value in exact.items()],items=items,total=count,offset=offset,limit=limit,basis='saved_prematch_picks',card_basis='yellow_plus_red_requires_all_four',card_market_definitions={'cards':'yellow_plus_red_requires_all_four','yellow_cards':'yellow_only_requires_both_teams','red_cards':'red_only_requires_both_teams'})


async def capture_upcoming(session,league_id):
    from datetime import timedelta
    from src.api.routes import match_query,match_response
    from src.models import utcnow
    from src.analytics.service import statistics,revision
    now=utcnow();query,_,_=match_query()
    query=query.where(League.enabled.is_(True),Match.league_id==league_id,Match.status=='scheduled',Match.kickoff_at>now,Match.kickoff_at<=now+timedelta(days=7))
    rows=(await session.execute(query.order_by(Match.kickoff_at,Match.id))).all();rev=await revision(session);created=False
    for row in rows:
        result=await statistics(session,row[0],match_response(row),now,rev)
        created=await capture(session,row[0],result['recommendations'],utcnow()) or created
    if created:await session.commit()
