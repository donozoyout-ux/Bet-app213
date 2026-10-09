"""Nullable-aware corner/card summaries and independent Poisson count estimates."""
import math
from datetime import timedelta
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import defer
from src.models import Match, MatchStatistics, MatchEvent, Referee
from src.jobs.worker import aware
from src.scrapers.goaloo.statistics import STAT_FIELDS


def summary(rows,team_id,metric,card_basis='yellow_plus_red'):
    paired=[];own=[];opponent=[];reds=[];all_cards=[];combined_own=[];combined_opponent=[]
    for match,stats in rows:
        if match.status!='finished' or stats is None or not stats.is_final:continue
        side='home' if match.home_team_id==team_id else 'away';other='away' if side=='home' else 'home'
        field='corners' if metric=='corners' else 'yellow_cards'
        a,b=getattr(stats,side+'_'+field),getattr(stats,other+'_'+field)
        if a is not None:own.append(a)
        if b is not None:opponent.append(b)
        if a is not None and b is not None:paired.append(a+b)
        red=getattr(stats,side+'_red_cards')
        if red is not None:reds.append(red)
        other_red=getattr(stats,other+'_red_cards')
        if a is not None and red is not None:combined_own.append(a+red)
        if b is not None and other_red is not None:combined_opponent.append(b+other_red)
        values=[stats.home_yellow_cards,stats.away_yellow_cards,stats.home_red_cards,stats.away_red_cards]
        if all(value is not None for value in values):all_cards.append(sum(values))
    avg=lambda values:sum(values)/len(values) if values else None
    thresholds=[7.5,8.5,9.5,10.5,11.5] if metric=='corners' else [2.5,3.5,4.5,5.5,6.5]
    totals=all_cards if metric=='cards' and card_basis=='yellow_plus_red' else paired
    own_values=combined_own if metric=='cards' and card_basis=='yellow_plus_red' else own
    opponent_values=combined_opponent if metric=='cards' and card_basis=='yellow_plus_red' else opponent
    rates={f'over_{str(t).replace(".","_")}':sum(v>t for v in totals)/len(totals) if totals else None for t in thresholds}
    return {'matches_considered':len(rows),'sample_size':len(totals),'for_sample_size':len(own_values),'against_sample_size':len(opponent_values),
            'for_avg':avg(own_values),'against_avg':avg(opponent_values),'total_avg':avg(totals),'over_rates':rates,
            'corners_for_avg':avg(own) if metric=='corners' else None,'corners_against_avg':avg(opponent) if metric=='corners' else None,
            'total_match_corners_avg':avg(paired) if metric=='corners' else None,
            'yellow_cards_for_avg':avg(own) if metric=='cards' else None,'yellow_cards_against_avg':avg(opponent) if metric=='cards' else None,
            'yellow_for_sample_size':len(own) if metric=='cards' else None,'yellow_against_sample_size':len(opponent) if metric=='cards' else None,
            'red_cards_for_avg':avg(reds) if metric=='cards' else None,'red_sample_size':len(reds),
            'total_match_cards_avg':avg(all_cards) if metric=='cards' else None,'all_card_sample_size':len(all_cards),
            'rate_basis':'corners' if metric=='corners' else card_basis}


def poisson_over(mean,threshold):
    if mean<0 or not math.isfinite(mean):return None
    probability=term=math.exp(-mean)
    for k in range(1,math.floor(threshold)+1):term*=mean/k;probability+=term
    return max(0,min(1,1-probability))


def count_prediction(rows,home_id,away_id,metric,card_basis='yellow_plus_red'):
    def values(match,stats):
        if stats is None:return None,None
        if metric=='corners':return stats.home_corners,stats.away_corners
        if card_basis=='yellow_plus_red':
            vals=[stats.home_yellow_cards,stats.away_yellow_cards,stats.home_red_cards,stats.away_red_cards]
            return (vals[0]+vals[2],vals[1]+vals[3]) if all(v is not None for v in vals) else (None,None)
        return stats.home_yellow_cards,stats.away_yellow_cards
    complete=[(m,s,values(m,s)) for m,s in rows if m.status=='finished' and s is not None and s.is_final and all(v is not None and v>=0 for v in values(m,s))]
    home=[r for r in complete if home_id in (r[0].home_team_id,r[0].away_team_id)][:10]
    away=[r for r in complete if away_id in (r[0].home_team_id,r[0].away_team_id)][:10]
    hs=[r for r in complete if r[0].home_team_id==home_id][:10]
    aws=[r for r in complete if r[0].away_team_id==away_id][:10]
    result={'status':'insufficient_data','sample_size':len({m.id for m,s,v in home+away+hs+aws}),
            'league_sample_size':min(200,len(complete)),'home_sample_size':len(home),'away_sample_size':len(away),
            'home_split_sample_size':len(hs),'away_split_sample_size':len(aws),
            'basis':'corners' if metric=='corners' else card_basis,'expected_home':None,'expected_away':None,'expected_total':None,'over_probabilities':{}}
    required={'league_sample_size':20,'home_sample_size':5,'away_sample_size':5,'home_split_sample_size':3,'away_split_sample_size':3}
    result['required_samples']=required
    result['insufficient_reasons']=[{'code':key,'actual':result[key],'required':minimum} for key,minimum in required.items() if result[key]<minimum]
    if result['insufficient_reasons']:return result
    baseline=complete[:200];bh=sum(v[0] for m,s,v in baseline)/len(baseline);ba=sum(v[1] for m,s,v in baseline)/len(baseline)
    if bh==0 or ba==0:
        result['insufficient_reasons']=[{'code':'zero_league_baseline'}]
        return result
    shrink=lambda total,n,prior:(total+5*prior)/(n+5)
    expected_home=shrink(sum(v[0] for m,s,v in hs),len(hs),bh)*shrink(sum(v[0] for m,s,v in aws),len(aws),bh)/bh
    expected_away=shrink(sum(v[1] for m,s,v in aws),len(aws),ba)*shrink(sum(v[1] for m,s,v in hs),len(hs),ba)/ba
    # Bounds ensure stable computation for corrupted/extreme input, not synthetic data.
    expected_home,expected_away=min(30,expected_home),min(30,expected_away)
    total=expected_home+expected_away;thresholds=[7.5,8.5,9.5,10.5,11.5] if metric=='corners' else [2.5,3.5,4.5,5.5,6.5]
    result.update(status='ok',expected_home=expected_home,expected_away=expected_away,expected_total=total,
                  over_probabilities={str(t):poisson_over(total,t) for t in thresholds})
    return result


async def additional_statistics(session,match,cutoff):
    basis='yellow_plus_red'
    history_key=(match.league_id,cutoff)
    history_cache=session.info.setdefault('recommendation_count_history',{})
    query=select(Match,MatchStatistics).options(defer(Match.raw),defer(MatchStatistics.raw)).outerjoin(MatchStatistics,and_(MatchStatistics.match_id==Match.id,MatchStatistics.is_final.is_(True),MatchStatistics.updated_at<=cutoff)).where(
        Match.league_id==match.league_id,Match.id!=match.id,Match.status=='finished',
        Match.kickoff_at<=cutoff-timedelta(hours=3),Match.kickoff_at>=cutoff-timedelta(days=3*366),
        Match.created_at<=cutoff,Match.updated_at<=cutoff,or_(Match.last_scraped_at.is_(None),Match.last_scraped_at<=cutoff),
        ).order_by(Match.kickoff_at.desc(),Match.id.desc())
    if history_key not in history_cache:history_cache[history_key]=(await session.execute(query)).all()
    rows=history_cache[history_key]
    result={}
    for metric in ['corners','cards']:
        sides={}
        for label,team in [('home',match.home_team_id),('away',match.away_team_id)]:
            own=[r for r in rows if team in (r[0].home_team_id,r[0].away_team_id)]
            sides[label]={'last_5':summary(own[:5],team,metric,basis),'last_10':summary(own[:10],team,metric,basis),
                          'season':summary([r for r in own if r[0].season_id==match.season_id],team,metric,basis),
                          'home_split':summary([r for r in own if r[0].home_team_id==team],team,metric,basis),
                          'away_split':summary([r for r in own if r[0].away_team_id==team],team,metric,basis)}
            for split in ['home_split','away_split']:sides[label][split]['status']='ok' if sides[label][split]['sample_size']>=3 else 'insufficient_data'
        prediction=count_prediction(rows,match.home_team_id,match.away_team_id,metric,basis)
        if match.kickoff_at is None:prediction.update(status='insufficient_data',expected_home=None,expected_away=None,expected_total=None,over_probabilities={})
        result[metric]={**sides,'prediction':prediction}
    observed_cache=session.info.get('board_statistics',{})
    observed=observed_cache[match.id] if match.id in observed_cache else await session.get(MatchStatistics,match.id)
    result['observed_match_statistics']={key:getattr(observed,key) for key in STAT_FIELDS} if observed and observed.collection_status=='available' else None
    result['observed_statistics_at']=aware(observed.updated_at) if observed else None
    event_cache=session.info.get('board_events',{})
    events=event_cache[match.id] if match.id in event_cache else (await session.scalars(select(MatchEvent).where(MatchEvent.match_id==match.id).order_by(MatchEvent.minute,MatchEvent.stoppage_minute,MatchEvent.id))).all()
    result['events']=[{'minute':e.minute,'stoppage_minute':e.stoppage_minute,'team_side':e.team_side,'event_type':e.event_type,
                       'player_name':e.player_name,'secondary_player_name':e.secondary_player_name} for e in events]
    result['referee']=None
    if match.referee_id:
        referee=await session.get(Referee,match.referee_id)
        if referee:
            history=(await session.execute(select(Match,MatchStatistics).join(MatchStatistics).where(
                Match.referee_id==referee.id,Match.id!=match.id,Match.status=='finished',MatchStatistics.is_final.is_(True),
                Match.kickoff_at<=cutoff-timedelta(hours=3),MatchStatistics.updated_at<=cutoff,Match.updated_at<=cutoff,
                Match.created_at<=cutoff,or_(Match.last_scraped_at.is_(None),Match.last_scraped_at<=cutoff),Match.referee_observed_at<=cutoff))).all()
            stats={'name':referee.name,'matches_officiated':len(history),'status':'ok' if len(history)>=10 else 'insufficient_data'}
            for name,fields in [('average_total_yellow_cards',['home_yellow_cards','away_yellow_cards']),('average_total_red_cards',['home_red_cards','away_red_cards']),
                                ('average_total_fouls',['home_fouls','away_fouls']),('home_yellow_average',['home_yellow_cards']),('away_yellow_average',['away_yellow_cards'])]:
                vals=[sum(getattr(s,f) for f in fields) for m,s in history if all(getattr(s,f) is not None for f in fields)]
                stats[name]=sum(vals)/len(vals) if len(vals)>=10 else None;stats[name+'_sample_size']=len(vals)
            if any(stats[key] is None for key in ['average_total_yellow_cards','average_total_red_cards','average_total_fouls']):stats['status']='insufficient_data'
            result['referee']=stats
    return result
