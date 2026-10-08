"""Deterministic quality selection; scores describe evidence, not calibrated accuracy.

Strong gate: probability >= .60, reliability >= .70, score >= .74,
each team >=8, each venue >=5, league >=50, completeness >=.60,
stability >=.50. Select at most one representative per correlation group.
"""
import math

PROBABILITY_MIN=.60
RELIABILITY_MIN=.70
SCORE_MIN=.74


def clamp(value):return max(0,min(1,value))


def rank_candidate(candidate):
    q=candidate['evidence'];p=candidate['probability']
    components={'sample_quality':clamp(min(q['home_sample'],q['away_sample'])/10),
                'venue_quality':clamp(min(q['home_venue'],q['away_venue'])/8),
                'baseline_quality':clamp(q['league_sample']/100),'stability':clamp(q['stability']),
                'season_agreement':clamp(q['season_agreement']),'completeness':clamp(q['completeness']),
                'market_availability':clamp(candidate.get('bookmaker_count',0)/3),
                'confidence_quality':{'high':1,'medium':.75,'low':.5}.get(q['confidence'],.5)}
    market=candidate.get('market_probability');difference=p-market if market is not None else None
    components['market_agreement']=clamp(1-abs(difference)/.25) if difference is not None else .5
    reliability=sum(components[key]*weight for key,weight in [('sample_quality',.25),('venue_quality',.15),
        ('baseline_quality',.10),('stability',.15),('season_agreement',.10),('completeness',.15),
        ('market_availability',.04),('market_agreement',.03),('confidence_quality',.03)])
    # Insurance selections are structurally more probable, not inherently more informative.
    convenience_penalty=.06 if candidate['market']=='double_chance' else 0
    extreme_penalty=max(0,p-.85)*.25
    score=clamp(.2*p+.8*reliability-convenience_penalty-extreme_penalty)
    passed=all([math.isfinite(p),p>=PROBABILITY_MIN,reliability>=RELIABILITY_MIN,score>=SCORE_MIN,
                min(q['home_sample'],q['away_sample'])>=8,min(q['home_venue'],q['away_venue'])>=5,
                q['league_sample']>=50,q['completeness']>=.6,q['stability']>=.5])
    confidence='high' if reliability>=.9 and q['season_agreement']>=.8 and candidate.get('bookmaker_count',0)>=2 and q.get('competition_type')!='national' else 'medium' if reliability>=.7 else 'low'
    return {**candidate,'score':score,'reliability':reliability,'confidence':confidence,'qualifies':passed,
            'sample_size':q['sample_size'],'score_components':components,'model_market_difference':difference,
            'penalties':{'insurance':convenience_penalty,'extreme_probability':extreme_penalty}}


def select_recommendations(candidates):
    ranked=sorted((rank_candidate(c) for c in candidates),key=lambda c:(-c['score'],-c['reliability'],c['id']))
    selected=[];groups=set()
    for candidate in ranked:
        if not candidate['qualifies'] or candidate['correlation_group'] in groups:continue
        selected.append(candidate);groups.add(candidate['correlation_group'])
        if len(selected)==3:break
    return ranked,selected


def market_matches(recommendations,market):
    families={'goals':{'goals','btts'},'result':{'result','double_chance'}}
    return not market or market=='all' or any(r['market'] in families.get(market,{market}) for r in recommendations)


def build_candidates(result,market_support=None):
    p=result['prediction'];extra=result.get('additional_statistics',{});market_support=market_support or {}
    candidates=[]
    def add(market,selection,label,probability,group,evidence):
        if probability is None or not math.isfinite(probability) or not 0<=probability<=1:return
        id=selection+'_'+market;support=market_support.get(id,{})
        candidates.append({'id':id,'market':market,'selection':selection,'label':label,'probability':probability,
                           'correlation_group':group,'evidence':evidence,
                           'market_probability':support.get('probability'),'bookmaker_count':support.get('bookmaker_count',0)})
    if p['status']=='ok':
        rules=p['confidence_rules'];season=result.get('ranking_evidence',{})
        evidence={'home_sample':p['home_sample_size'],'away_sample':p['away_sample_size'],
                  'home_venue':p['home_venue_sample_size'],'away_venue':p['away_venue_sample_size'],
                  'league_sample':p['league_sample_size'],'sample_size':p['sample_size'],
                  'stability':season.get('goal_stability',1 if rules.get('recent_longer_form_agree') else .4),
                  'season_agreement':season.get('goal_season_agreement',.5),
                  'completeness':min(p['home_sample_size'],p['away_sample_size'])/10,
                  'confidence':p['confidence'],'competition_type':result['match']['competition_type']}
        for selection,label in [('home','Ev Sahibi'),('draw','Beraberlik'),('away','Deplasman')]:add('result',selection,label,p[selection+'_probability'],'outcome',evidence)
        for selection,label,probability in [('1x','1X',p['home_probability']+p['draw_probability']),('x2','X2',p['draw_probability']+p['away_probability']),('12','12',p['home_probability']+p['away_probability'])]:add('double_chance',selection,label,probability,'outcome',evidence)
        for line in ['1_5','2_5','3_5']:
            for direction,label in [('over','Üst'),('under','Alt')]:add('goals',direction+'_'+line,line.replace('_','.')+' Gol '+label,p.get(direction+'_'+line.replace('_','')+'_probability'),'goal_environment',evidence)
        add('btts','yes','KG Var',p['btts_probability'],'goal_environment',evidence)
        add('btts','no','KG Yok',p['no_btts_probability'],'goal_environment',evidence)
        for candidate in result.get('asian_handicap_candidates',[]):add('asian_handicap',candidate['selection'],candidate['label'],candidate['probability'],'outcome',evidence)
    for metric,label,lines in [('corners','Korner',['8_5','9_5','10_5']),('cards','Kart',['3_5','4_5','5_5'])]:
        values=extra.get(metric);cp=values.get('prediction') if values else None
        if not cp or cp['status']!='ok':continue
        ratios=[];stabilities=[];seasons=[]
        for side in ['home','away']:
            five,ten,year=values[side]['last_5'],values[side]['last_10'],values[side]['season']
            ratios.append(ten['sample_size']/max(1,ten['matches_considered']))
            def agree(a,b):return clamp(1-abs(a-b)/max(1,abs(b))) if a is not None and b is not None else .5
            stabilities.append(agree(five['total_avg'],ten['total_avg']))
            seasons.append(agree(five['total_avg'],year['total_avg']))
        stable=min(stabilities);season_agreement=min(seasons);complete=min(ratios)
        base_confidence='medium' if min(cp['home_sample_size'],cp['away_sample_size'])>=8 and min(cp['home_split_sample_size'],cp['away_split_sample_size'])>=5 and cp['league_sample_size']>=50 and stable>=.6 and season_agreement>=.6 and complete>=.8 else 'low'
        evidence={'home_sample':cp['home_sample_size'],'away_sample':cp['away_sample_size'],
                  'home_venue':cp['home_split_sample_size'],'away_venue':cp['away_split_sample_size'],
                  'league_sample':cp['league_sample_size'],'sample_size':cp['sample_size'],
                  'stability':stable,'season_agreement':season_agreement,'completeness':complete,
                  'confidence':base_confidence,'competition_type':result['match']['competition_type']}
        for line in lines:
            over=cp['over_probabilities'].get(line.replace('_','.'))
            if over is None:continue
            add(metric,'over_'+line,line.replace('_','.')+' '+label+' Üst',over,metric,evidence)
            add(metric,'under_'+line,line.replace('_','.')+' '+label+' Alt',1-over,metric,evidence)
    return candidates
