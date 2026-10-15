"""Forward-only, immutable prematch picks; settlement uses published final data."""
from collections import defaultdict
import math
from sqlalchemy import select
from sqlalchemy.orm import defer
from src.models import PredictionSnapshot, Match, MatchStatistics, League
from src.jobs.worker import aware
from src.analytics.recommendations import market_matches


async def capture(session, match, picks, now):
    from src.analytics.board_cache import MODEL_VERSION
    if match.status != 'scheduled' or not match.kickoff_at or aware(match.kickoff_at) <= now or not picks:return False
    known=session.info.get('captured_matches')
    if (match.id in known) if known is not None else (await session.get(PredictionSnapshot,match.id)):
        return False
    if session.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    result=await session.execute(insert(PredictionSnapshot).values(match_id=match.id,league_id=match.league_id,captured_at=now,model_version=MODEL_VERSION,picks=picks).on_conflict_do_nothing(index_elements=['match_id']))
    if known is not None:known.add(match.id)
    return result.rowcount > 0


async def capture_many(session, observations, now):
    """Publish a board atomically without an INSERT round trip per match."""
    from src.analytics.board_cache import MODEL_VERSION
    known = session.info.get('captured_matches', set())
    records = [dict(match_id=m.id, league_id=m.league_id, captured_at=now,
                    model_version=MODEL_VERSION, picks=picks)
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


def _settle(match, stats, pick):
    if match.status in {'cancelled','abandoned'}:return 'void',None
    if match.status != 'finished':return 'pending',None
    market,selection=pick['market'],pick['selection']
    if market in {'corners','cards','yellow_cards','red_cards'}:
        fields=['home_corners','away_corners'] if market=='corners' else ['home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards']
        if market in {'yellow_cards','red_cards'}:fields=['home_'+market,'away_'+market]
        if not stats or not stats.is_final or any(getattr(stats,f) is None for f in fields):return 'awaiting_data',None
        if any(getattr(stats,f)<0 for f in fields):return 'awaiting_data',None
        total=sum(getattr(stats,f) for f in fields)
    elif market in {'first_half_goals','second_half_goals'}:
        if match.ht_home is None or match.ht_away is None:
            return 'awaiting_data',None
        if match.ht_home < 0 or match.ht_away < 0:
            return 'void',None
        if market == 'first_half_goals':
            total = match.ht_home + match.ht_away
        else:
            if match.ft_home is None or match.ft_away is None:return 'awaiting_data',None
            if match.ft_home < match.ht_home or match.ft_away < match.ht_away:return 'void',None
            total = (match.ft_home - match.ht_home) + (match.ft_away - match.ht_away)
    else:
        if match.ft_home is None or match.ft_away is None:return 'awaiting_data',None
        home,away=match.ft_home,match.ft_away
        if home<0 or away<0:return 'awaiting_data',None
        total=home+away
    if market in {'goals','corners','cards','yellow_cards','red_cards','first_half_goals','second_half_goals'}:
        direction,a,b=selection.split('_');line=float(a+'.'+b)
        if total==line:return 'void',total
        won=total>line if direction=='over' else total<line;actual=total
    elif market=='btts':won=(home>0 and away>0)==(selection=='yes');actual=f'{home}-{away}'
    elif market=='result':won={'home':home>away,'draw':home==away,'away':home<away}[selection];actual=f'{home}-{away}'
    elif market=='double_chance':won={'1x':home>=away,'x2':home<=away,'12':home!=away}[selection];actual=f'{home}-{away}'
    elif market=='asian_handicap':
        side,sign,a,b=selection.split('_');line=float(a+'.'+b)*(1 if sign=='plus' else -1);difference=home-away if side=='home' else away-home
        if difference+line==0:return 'void',f'{home}-{away}'
        won=difference+line>0;actual=f'{home}-{away}'
    else:return 'awaiting_data',None
    return ('won' if won else 'lost'),actual


def settle(match, stats, pick):
    """Reject unknown/malformed imported selections rather than guessing a result."""
    import re
    market = pick.get('market')
    selection = pick.get('selection')
    totals = {'goals','corners','cards','yellow_cards','red_cards','first_half_goals','second_half_goals'}
    choices = {'btts': {'yes','no'}, 'result': {'home','draw','away'}, 'double_chance': {'1x','x2','12'}}
    valid = isinstance(selection, str) and (
        (market in totals and re.fullmatch(r'(over|under)_[0-9]+_[05]', selection)) or
        (market == 'asian_handicap' and re.fullmatch(r'(home|away)_(plus|minus)_[0-9]+_[05]', selection)) or
        (market in choices and selection in choices[market]))
    if not valid:
        return 'awaiting_data', None
    return _settle(match, stats, pick)


def tally():return dict(total=0,settled=0,won=0,lost=0,pending=0,awaiting_data=0,void=0,reliability_sum=0,sample_sum=0,last_10=[],probability_sum=0,brier_sum=0,calibrated_samples=0,calibration={},priced_samples=0,profit=0)


def add(result,pick,outcome,captured_at=None):
    result['total']+=1;result['reliability_sum']+=pick['reliability'];result['sample_sum']+=pick['sample_size']
    result[outcome]+=1
    if outcome in {'won','lost'}:result['settled']+=1
    if len(result['last_10'])<10:result['last_10'].append(outcome)
    if outcome not in {'won','lost'}:return
    p=pick.get('probability')
    if isinstance(p,(int,float)) and math.isfinite(p) and 0<=p<=1:
        y=int(outcome=='won');result['calibrated_samples']+=1
        result['probability_sum']+=p;result['brier_sum']+=(p-y)**2
        bin=min(int(p*10),9)
        bucket=result['calibration'].setdefault(bin,dict(samples=0,won=0,probability_sum=0))
        bucket['samples']+=1;bucket['won']+=y;bucket['probability_sum']+=p
    # Prices must come from the immutable prematch snapshot, never today's database.
    vo=pick.get('verified_odds') or {};price=vo.get('best_price');stamp=vo.get('observed_at')
    try:
        from datetime import datetime,timedelta
        observed=aware(datetime.fromisoformat(str(stamp).replace('Z','+00:00')))
        valid=vo.get('has_odds') is True and vo.get('stage')=='latest' and captured_at is not None and math.isfinite(price) and price>1 and observed<=aware(captured_at) and aware(captured_at)-observed<=timedelta(minutes=15)
    except (ValueError,TypeError):valid=False
    if valid:
        result['priced_samples']+=1;result['profit']+=price-1 if outcome=='won' else -1


def finish(result):
    result=dict(result);n=result['total'];settled=result['settled']
    result['success_rate']=result['won']/settled if settled else None
    result['average_reliability']=result.pop('reliability_sum')/n if n else None
    result['average_sample_size']=result.pop('sample_sum')/n if n else None
    calibrated=result['calibrated_samples']
    probability_sum=result.pop('probability_sum');brier_sum=result.pop('brier_sum')
    result['mean_probability']=probability_sum/calibrated if calibrated else None
    result['brier_score']=brier_sum/calibrated if calibrated else None
    result['calibration']=[dict(lower=key/10,upper=(key+1)/10,samples=b['samples'],observed_rate=b['won']/b['samples'],mean_probability=b['probability_sum']/b['samples']) for key,b in sorted(result['calibration'].items())]
    result['roi']=result['profit']/result['priced_samples'] if result['priced_samples'] else None
    return result


async def performance(session, league=None, start=None, end=None, market='all', exact_market=None, confidence=None, limit=50, offset=0):
    from src.api.routes import match_query,match_response
    query,_,_=match_query()
    query=query.add_columns(PredictionSnapshot,MatchStatistics).options(defer(Match.raw),defer(MatchStatistics.raw)).join(PredictionSnapshot,PredictionSnapshot.match_id==Match.id).outerjoin(MatchStatistics,MatchStatistics.match_id==Match.id).where(League.enabled.is_(True))
    if league:query=query.where(League.id==int(league)) if league.isdigit() else query.where(League.name.ilike('%'+league+'%'))
    if start:query=query.where(PredictionSnapshot.captured_at>=start)
    if end:query=query.where(PredictionSnapshot.captured_at<end)
    query=query.order_by(PredictionSnapshot.captured_at.desc(),Match.id.desc()).execution_options(yield_per=200)
    from src.analytics.settlements import decisions_many
    frozen_rows=await decisions_many(session,query.with_only_columns(Match.id).order_by(None))
    summary=tally();families=defaultdict(tally);exact=defaultdict(tally);labels={};items=[];count=0
    cohorts={name:tally() for name in ('all_saved','probability_70','reliability_80','quality_80','combined_70_80_80')}
    async for row in await session.stream(query):
        match=row[0];snapshot,stats=row[-2:]
        # Never accept a record captured at/after kickoff, including imported bad rows.
        if not match.kickoff_at or aware(snapshot.captured_at)>=aware(match.kickoff_at):continue
        frozen=frozen_rows.get(match.id,{})
        for index,pick in enumerate(snapshot.picks):
            if not market_matches([pick],market) or exact_market and pick['id']!=exact_market or confidence and pick['confidence']!=confidence:continue
            saved=frozen.get(index)
            outcome,actual=(saved.outcome,saved.actual['value']) if saved else settle(match,stats,pick)
            add(summary,pick,outcome,snapshot.captured_at);add(families[pick['market']],pick,outcome,snapshot.captured_at);add(exact[pick['id']],pick,outcome,snapshot.captured_at);labels[pick['id']]=(pick['market'],pick['label'])
            gates={'probability_70':pick.get('probability',0)>=.7,'reliability_80':pick.get('reliability',0)>=.8,'quality_80':pick.get('score',0)>=.8}
            for name,accept in {'all_saved':True,**gates,'combined_70_80_80':all(gates.values())}.items():
                if accept:add(cohorts[name],pick,outcome,snapshot.captured_at)
            if offset<=count<offset+limit:items.append(dict(match=match_response(row[:5]),recommendation=pick,captured_at=aware(snapshot.captured_at),model_version=snapshot.model_version,outcome=outcome,actual=actual,settlement_basis='recorded' if saved else 'unrecorded_evaluation',settlement_revision=saved.revision if saved else None))
            count+=1
    from src.diagnostics import statistics_coverage
    coverage=await statistics_coverage(session)
    return dict(threshold_comparison={name:finish(value) for name,value in cohorts.items()},price_basis='immutable_fresh_prematch_snapshot',stats_coverage_by_league=coverage,availability='saved_prematch_picks' if count else 'no_saved_prematch_picks',summary=finish(summary),families=[dict(market=key,**finish(value)) for key,value in families.items()],markets=[dict(id=key,market=labels[key][0],label=labels[key][1],**finish(value)) for key,value in exact.items()],items=items,total=count,offset=offset,limit=limit,basis='saved_prematch_picks',card_basis='yellow_plus_red_requires_all_four',card_market_definitions={'cards':'yellow_plus_red_requires_all_four','yellow_cards':'yellow_only_requires_both_teams','red_cards':'red_only_requires_both_teams'})


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
