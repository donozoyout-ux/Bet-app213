"""Deterministic marginal portfolio selection, without a leg or price cap.

Risk is an explicit policy proxy, NOT a calibrated joint win probability.
Evidence discounts and concentration charges intentionally avoid assuming that
every different fixture is independent. All candidates are examined; a bounded
greedy search controls CPU cost instead of silently truncating the input pool.
"""
import math
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from src.analytics.coupon import coupon_qualified

POLICY_VERSION='dynamic-marginal-v1'
RISK_BUDGET=1.6
MAX_EVALUATIONS=100_000
AVERSION={'balanced':.12,'cautious':.20,'value':.08}


def teams(match):
    return {str(match.get(side+'_team_id') or match.get(side+'_team','')).strip().casefold()
            for side in ('home','away')} - {''}


def select_portfolio(records,now,strategy='balanced'):
    if strategy not in AVERSION:raise ValueError('Unsupported coupon strategy')
    today=now.astimezone(ZoneInfo('Europe/Istanbul')).date()
    candidates=[];decisions=[]
    for item in records:
        m=item['match'];r=item['recommendation'];q=item['quote']
        decision=dict(match_id=m['id'],recommendation_id=r['id'],label=r.get('label',r['id']),selected=False,reason=None)
        decisions.append(decision)
        kickoff=datetime.fromisoformat(str(m['kickoff_at']).replace('Z','+00:00'))
        if kickoff.tzinfo is None:kickoff=kickoff.replace(tzinfo=timezone.utc)
        if kickoff<=now or kickoff.astimezone(ZoneInfo('Europe/Istanbul')).date()!=today:
            decision['reason']='outside_daily_window';continue
        if not coupon_qualified(r) or not all(0<r[k]<=1 for k in ('probability','reliability','score')):decision['reason']='quality_rejected';continue
        price=q.get('price')
        if type(price) not in (int,float) or not math.isfinite(price) or price<=1:
            decision['reason']='price_rejected';continue
        try:
            stamp=datetime.fromisoformat(str(q.get('observed_at')).replace('Z','+00:00'))
            expiry=datetime.fromisoformat(str(q.get('expires_at')).replace('Z','+00:00'))
            fresh=stamp.tzinfo is not None and expiry.tzinfo is not None and 0<=(now-stamp).total_seconds()<=900 and expiry>now
        except (ValueError,TypeError):fresh=False
        if not fresh or not q.get('eligible') or q.get('bookmaker') not in ('Crown','Bet365','Sbobet'):
            decision['reason']='price_rejected';continue
        identities=teams(m)
        if len(identities)!=2:decision['reason']='team_identity_missing';continue
        # Reliability and quality scores are evidence indicators, not probabilities.
        quality=min(r['reliability'],r['score'])
        adjusted=r['probability']*(.5+.5*quality)
        if not 0<adjusted<=1:decision['reason']='quality_rejected';continue
        base_risk=-math.log(adjusted)
        # log(p)+log(price) avoids overflow for genuinely very large source prices.
        value=math.tanh(math.log(r['probability'])+math.log(price))*quality
        decision.update(base_risk=base_risk,quality=quality,model_value_indicator=value)
        candidates.append(dict(item=item,decision=decision,teams=identities,risk=base_risk,value=value,
            league=m.get('league_id'),family=r.get('correlation_group') or r['market']))
    # Stable tie order is independent of the database/API row order.
    candidates.sort(key=lambda c:(c['item']['match']['id'],c['item']['recommendation']['id'],c['item']['quote'].get('bookmaker','')))
    chosen=[];matches=set();used_teams=set();leagues=Counter();families=Counter()
    risk=0.;quality_total=0.;evaluations=0;remaining=list(candidates);budget_hit=False
    while remaining:
        best=None
        # A round is atomic: never choose from only the first part of a round.
        if evaluations+len(remaining)>MAX_EVALUATIONS:
            budget_hit=True;break
        for c in remaining:
            evaluations+=1;d=c['decision'];mid=c['item']['match']['id']
            if mid in matches:d['reason']='same_match';continue
            if c['teams'] & used_teams:d['reason']='shared_team';continue
            concentration=.08*leagues[c['league']]+.04*families[c['family']]
            added=c['risk']+concentration
            average=quality_total/len(chosen) if chosen else c['decision']['quality']
            dilution=max(0,average-c['decision']['quality'])*.5
            gain=c['value']-AVERSION[strategy]*added*(1+risk/RISK_BUDGET)-dilution
            d.update(marginal_risk=added,marginal_utility=gain,portfolio_risk_if_added=risk+added,
                portfolio_quality_before=average,portfolio_quality_if_added=(quality_total+c['decision']['quality'])/(len(chosen)+1),quality_dilution_penalty=dilution)
            if risk+added>RISK_BUDGET+1e-12:d['reason']='combined_risk_budget';continue
            if gain<=0:d['reason']='non_positive_marginal_utility';continue
            if best is None or gain>best['decision']['marginal_utility']+1e-12:best=c
        if best is None:break
        d=best['decision'];d.update(selected=True,reason='positive_marginal_utility',portfolio_risk_before=risk,selection_order=len(chosen)+1)
        risk+=d['marginal_risk'];d['portfolio_risk_after']=risk
        quality_total+=d['quality']
        best['item']={**best['item'],'selection_explanation':dict(d)}
        chosen.append(best['item']);matches.add(best['item']['match']['id']);used_teams.update(best['teams'])
        leagues[best['league']]+=1;families[best['family']]+=1
        remaining=[c for c in remaining if c is not best]
    if budget_hit:
        for c in remaining:c['decision']['reason']='evaluation_budget'
    # Re-evaluate final hard conflicts for alternatives left after the last round.
    for c in candidates:
        if c['decision']['selected']:continue
        if c['item']['match']['id'] in matches:c['decision']['reason']='same_match'
        elif c['teams'] & used_teams:c['decision']['reason']='shared_team'
    audit=dict(policy_version=POLICY_VERSION,strategy=strategy,selection_mode='dynamic',
        risk_indicator=risk,risk_budget=RISK_BUDGET,risk_basis='uncalibrated_evidence_and_concentration_proxy',
        portfolio_quality=quality_total/len(chosen) if chosen else None,
        evaluations=evaluations,evaluation_budget=MAX_EVALUATIONS,evaluation_budget_exhausted=budget_hit,
        qualified_candidates=len(candidates),selected_matches=len(chosen),decisions=decisions,
        rejections=dict(Counter(d['reason'] for d in decisions if not d['selected'])))
    return sorted(chosen,key=lambda i:(str(i['match']['kickoff_at']),i['match']['id'])),audit
