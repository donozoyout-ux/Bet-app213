"""As-of data selection and bounded process-local cache; no scraper side effects."""
from collections import OrderedDict
from datetime import timedelta
import time
from sqlalchemy import select, func, or_, event
from sqlalchemy.orm import aliased, Session
from src.models import Match, League, Team, Bookmaker, Odds1X2, MatchStatistics, AsianHandicap, AsianTotals
from src.jobs.worker import aware
from .predictions import Result, predict, implied_probabilities, team_stats, handicap_probability
from src.match_views import LIVE_STATUSES
from .match_statistics import additional_statistics
from .recommendations import build_candidates,select_recommendations

CACHE_SECONDS = 600
_cache = OrderedDict()


@event.listens_for(Session, 'after_commit')
def invalidate_after_commit(session):
    # Catch updates below a global MAX timestamp, not just count/MAX changes.
    # Fires only after writes become visible; no pre-commit stale-cache race.
    _cache.clear()


async def revision(session):
    matches = (await session.execute(select(func.count(Match.id), func.max(Match.updated_at), func.max(Match.last_scraped_at)))).one()
    odds = (await session.execute(select(func.count(Odds1X2.id), func.max(Odds1X2.updated_at)))).one()
    stats=(await session.execute(select(func.count(MatchStatistics.match_id),func.max(MatchStatistics.updated_at)))).one()
    asian=(await session.execute(select(func.count(AsianHandicap.id),func.max(AsianHandicap.updated_at)))).one()
    totals=(await session.execute(select(func.count(AsianTotals.id),func.max(AsianTotals.updated_at)))).one()
    return tuple(matches) + tuple(odds) + tuple(stats)+tuple(asian)+tuple(totals)


async def statistics(session, match, context, now, data_revision=None):
    cutoff = min(now, aware(match.kickoff_at)) if match.kickoff_at else now
    historical = match.kickoff_at is not None and aware(match.kickoff_at) <= now
    data_revision = data_revision if data_revision is not None else await revision(session)
    time_key = aware(match.kickoff_at).isoformat() if historical else int(cutoff.timestamp() // CACHE_SECONDS)
    key = (session.bind, match.id, match.league_id, match.home_team_id, match.away_team_id,
           context['competition_type'], aware(match.kickoff_at), match.status, historical, time_key, data_revision)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        _cache.move_to_end(key)
        return cached[1]
    home, away = aliased(Team), aliased(Team)
    # Observation stamps prevent use of results collected/corrected after cutoff.
    # No precise finish timestamp exists: a conservative three-hour kickoff buffer
    # also excludes overlapping games from usable full-time history.
    query = select(Match.id, Match.home_team_id, Match.away_team_id, Match.ft_home, Match.ft_away,
                   Match.kickoff_at, home.name, away.name, Match.season_id).join(home, Match.home_team_id == home.id).join(away, Match.away_team_id == away.id).where(
        Match.league_id == match.league_id, Match.id != match.id, Match.status == 'finished',
        Match.ft_home.is_not(None), Match.ft_away.is_not(None), Match.ft_home >= 0, Match.ft_away >= 0,
        Match.kickoff_at <= cutoff - timedelta(hours=3), Match.kickoff_at >= cutoff - timedelta(days=3*366),
        Match.created_at <= cutoff, Match.updated_at <= cutoff,
        or_(Match.last_scraped_at.is_(None), Match.last_scraped_at <= cutoff)).order_by(Match.kickoff_at.desc(), Match.id.desc()).limit(2000)
    history_key=(match.league_id,cutoff)
    history_cache=session.info.setdefault('recommendation_history',{})
    if history_key not in history_cache:
        rows=(await session.execute(query)).all()
        history_cache[history_key]=[Result(*row[:5],aware(row[5]),row[6],row[7],row[8]) for row in rows]
    history=history_cache[history_key]
    markets = (await session.execute(select(Odds1X2, Bookmaker.name).join(Bookmaker).where(Odds1X2.match_id == match.id, Bookmaker.name.in_(['Crown','Bet365','Sbobet'])))).all()
    bookmakers, valid = [], []
    for market, name in markets:
        closing = any(getattr(market, f'closing_{field}') is not None for field in ('home','draw','away'))
        price_stage = 'closing' if match.status == 'finished' or match.status in LIVE_STATUSES and closing else 'latest'
        opening = {field: getattr(market, f'opening_{field}') for field in ('home','draw','away')}
        prices = {field: getattr(market, f'{price_stage}_{field}') for field in ('home','draw','away')}
        available = aware(market.updated_at) <= cutoff
        normalized = implied_probabilities(prices) if available else None
        bookmakers.append({'bookmaker': name, 'opening': opening, 'prices': prices, 'stage': price_stage,
                           'observed_at': aware(market.updated_at), 'available_as_of': available,
                           'implied_probabilities': normalized})
        if normalized: valid.append(normalized)
    consensus = {field: sum(book[field] for book in valid) / len(valid) for field in ('home','draw','away')} if valid else None
    if consensus: consensus['bookmaker_count'] = len(valid)
    support={}
    if consensus:
        for field in ['home','draw','away']:support[field+'_result']={'probability':consensus[field],'bookmaker_count':len(valid)}
        for selection,fields in [('1x',['home','draw']),('x2',['draw','away']),('12',['home','away'])]:support[selection+'_double_chance']={'probability':sum(consensus[field] for field in fields),'bookmaker_count':len(valid)}
    prediction, extra = predict(history, match.home_team_id, match.away_team_id, context['competition_type'], consensus)
    if match.kickoff_at is None:
        prediction = {key: value for key,value in prediction.items() if 'probability' not in key and not key.startswith('expected_')}
        prediction.update(status='insufficient_data', confidence='low', model_market_difference=None)
    additional=await additional_statistics(session,match,cutoff)
    for metric in ['corners','cards']:
        p=additional[metric]['prediction'];prediction[metric+'_status']=p['status']
        prediction['expected_home_'+metric]=p['expected_home'];prediction['expected_away_'+metric]=p['expected_away'];prediction['expected_total_'+metric]=p['expected_total']
        for threshold in ([8.5,9.5,10.5] if metric=='corners' else [3.5,4.5,5.5]):
            prediction['over_'+str(threshold).replace('.','_')+'_'+metric+'_probability']=p['over_probabilities'].get(str(threshold))
    prediction['card_basis']=additional['cards']['prediction']['basis']
    def agree(a,b):return max(0,1-abs(a-b)/max(1,abs(b))) if a is not None and b is not None else .5
    stability=[];season_agreement=[]
    for side,team in [('home',match.home_team_id),('away',match.away_team_id)]:
        five,ten=extra[side+'_form'],extra[side+'_last_10']
        season_stats=team_stats([row for row in history if row.season_id==match.season_id and team in (row.home_id,row.away_id)],team)
        stability.append(min(agree(five['avg_goals_for'],ten['avg_goals_for']),agree(five['avg_goals_against'],ten['avg_goals_against'])))
        season_agreement.append(min(agree(five['avg_goals_for'],season_stats['avg_goals_for']),agree(five['avg_goals_against'],season_stats['avg_goals_against'])) if season_stats['sample_size']>=5 else .5)
    asian_candidates={};paired_support={}
    if prediction['status']=='ok':
        for model,metric in [(AsianTotals,'goals'),(AsianHandicap,'asian_handicap')]:
            price_rows=(await session.execute(select(model,Bookmaker.name).join(Bookmaker).where(model.match_id==match.id,model.updated_at<=cutoff,Bookmaker.name.in_(['Crown','Bet365','Sbobet'])))).all()
            for row,name in price_rows:
                stage='closing' if match.status=='finished' else 'latest'
                line=getattr(row,stage+'_line')
                if line is None:continue
                left,right=('over','under') if metric=='goals' else ('home','away')
                prices=[getattr(row,stage+'_'+key) for key in (left,right)]
                import math
                if any(value is None or not math.isfinite(value) or value<=1 for value in prices):continue
                total=sum(1/value for value in prices)
                for index,side in enumerate((left,right)):
                    if metric=='goals':
                        if line not in (1.5,2.5,3.5):continue
                        selection=side+'_'+str(line).replace('.','_')
                    else:
                        signed=line if side=='home' else -line
                        probability=handicap_probability(prediction['expected_home_goals'],prediction['expected_away_goals'],signed,side)
                        if probability is None:continue
                        selection=side+('_plus_' if signed>=0 else '_minus_')+str(abs(signed)).replace('.','_')
                        asian_candidates[selection]={'selection':selection,'label':('Ev Sahibi' if side=='home' else 'Deplasman')+f' {signed:+g} Handikap','probability':probability}
                    paired_support.setdefault(selection+'_'+metric,[]).append((1/prices[index])/total)
        for id,values in paired_support.items():support[id]={'probability':sum(values)/len(values),'bookmaker_count':len(values)}
    h2h_rows = [row for row in history if {row.home_id, row.away_id} == {match.home_team_id, match.away_team_id}][:5]
    h2h_stats = predict_h2h(h2h_rows, match.home_team_id)
    result = {'match': context, 'prediction': prediction, **extra, 'additional_statistics':additional, 'h2h': h2h_stats['matches'],
              'h2h_summary': {key: value for key, value in h2h_stats.items() if key != 'matches'},
              'bookmakers': bookmakers, 'bookmaker_consensus': consensus, 'as_of': cutoff,
              'historical': historical, 'cache_seconds': CACHE_SECONDS,
              'methodology': 'Independent Poisson; venue rates shrunk with weight 5 toward this competition only. Minimum: league 20, each team 5, each venue 3. Results require observation stamps before cutoff and a three-hour completion buffer. No measured xG, event statistics or ML.'}
    result['ranking_evidence']={'goal_stability':min(stability),'goal_season_agreement':min(season_agreement)}
    result['asian_handicap_candidates']=list(asian_candidates.values())
    candidates=build_candidates(result,support)
    result['candidates'],result['recommendations']=select_recommendations(candidates)
    result['strongest_prediction']=result['recommendations'][0]['id'] if result['recommendations'] else None
    _cache[key] = (time.monotonic(), result)
    _cache.move_to_end(key)
    while len(_cache) > 128: _cache.popitem(last=False)
    return result


def predict_h2h(rows, team_id):
    from .predictions import team_stats
    result = team_stats(rows, team_id)
    result['sufficient'] = len(rows) >= 2
    return result
