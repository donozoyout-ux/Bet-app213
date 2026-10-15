"""Exact-ID coverage and an OFFLINE opponent-adjusted UEFA challenger.

No dependency from prediction selection to the challenger. Observation stamps
are mandatory: archive backfills cannot manufacture historical availability.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import math
from statistics import mean
from .predictions import Result, predict, MIN_TEAM, MIN_SPLIT, MIN_LEAGUE

UEFA = {103, 113, 2187}
VERSION = 'uefa-opponent-elo-v1-research'


def stamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def valid(row):
    return (row['status'] == 'finished' and row['kickoff_at'] is not None
            and all(isinstance(row[k], int) and not isinstance(row[k], bool)
                    and row[k] >= 0 for k in ('ft_home', 'ft_away')))


def usable(row, cutoff):
    kickoff = stamp(row['kickoff_at'])
    return (valid(row) and cutoff - timedelta(days=3*366) <= kickoff <= cutoff-timedelta(hours=3)
            and all(stamp(row.get(k)) is not None and stamp(row[k]) <= cutoff
                    for k in ('created_at', 'updated_at'))
            and (row.get('last_scraped_at') is None or stamp(row['last_scraped_at']) <= cutoff))


def ordered(rows):
    return sorted(rows, key=lambda r: (stamp(r['kickoff_at']), r['id']))


def team_coverage(history, target, team, side, leagues):
    own = [r for r in history if team in (r['home_team_id'], r['away_team_id'])]
    cup = [r for r in own if r['league_id'] == target['league_id']]
    domestic = [r for r in own if leagues[r['league_id']]['schedule_format'] == 'league'
                and leagues[r['league_id']]['external_id'] not in UEFA]
    sources = sorted({r['league_id'] for r in domestic})
    venue_key = side+'_team_id'
    reasons = []
    if len(cup) < MIN_TEAM: reasons.append('Avrupa turnuvası takım geçmişi yetersiz')
    if sum(r[venue_key] == team for r in cup) < MIN_SPLIT: reasons.append('Avrupa ev/deplasman geçmişi yetersiz')
    if not domestic: reasons.append('Goaloo takım kimliğiyle eşleşen yerel sonuç yok; kaynak/kimlik doğrulaması gerekli')
    elif len(domestic) < MIN_TEAM: reasons.append('Yerel lig takım geçmişi yetersiz')
    if sum(r[venue_key] == team for r in domestic) < MIN_SPLIT: reasons.append('Yerel ev/deplasman geçmişi yetersiz')
    return dict(team_id=team, europe_matches=len(cup), europe_home=sum(r['home_team_id']==team for r in cup),
        europe_away=sum(r['away_team_id']==team for r in cup), domestic_matches=len(domestic),
        domestic_home=sum(r['home_team_id']==team for r in domestic), domestic_away=sum(r['away_team_id']==team for r in domestic),
        domestic_leagues=[dict(league_id=i, external_id=leagues[i]['external_id'], name=leagues[i]['name'],
            enabled=leagues[i]['enabled']) for i in sources], missing_reasons=reasons)


def baseline(history, target):
    rows = ordered(r for r in history if r['league_id'] == target['league_id'])[-2000:]
    results = [Result(r['id'], r['home_team_id'], r['away_team_id'], r['ft_home'], r['ft_away'], stamp(r['kickoff_at'])) for r in rows]
    return predict(results, target['home_team_id'], target['away_team_id'], 'club')[0]


def challenger(history, target, leagues):
    """Sequential Elo with learned cross-league offsets, never pooled goal means.

    Team ratings update against opponent strength; league offsets update ONLY
    from preceding cross-league UEFA matches, after exact-ID domestic membership
    is observed. Unconnected leagues abstain. Fixed research hyperparameters.
    """
    ratings=defaultdict(float); offsets=defaultdict(float); membership={}
    counts=Counter(); venues=Counter(); league_counts=Counter(); bridges=Counter()
    graph=defaultdict(set); draws=0; games=0
    for r in ordered(history):
        league=leagues[r['league_id']]; h,a=r['home_team_id'],r['away_team_id']
        domestic=league['schedule_format']=='league' and league['external_id'] not in UEFA
        if not domestic and league['external_id'] not in UEFA: continue
        if domestic:
            membership[h]=membership[a]=r['league_id']
            counts[h]+=1; counts[a]+=1; venues[h,'home']+=1; venues[a,'away']+=1
            league_counts[r['league_id']]+=1
        lh,la=membership.get(h),membership.get(a)
        if lh is None or la is None: continue
        gap=ratings[h]-ratings[a]+offsets[lh]-offsets[la]+60
        expected=1/(1+10**(-max(-1200,min(1200,gap))/400))
        actual=1 if r['ft_home']>r['ft_away'] else 0 if r['ft_home']<r['ft_away'] else .5
        change=20*(actual-expected)
        ratings[h]+=change; ratings[a]-=change
        if lh!=la and not domestic:
            offsets[lh]+=change/4; offsets[la]-=change/4
            bridges[lh]+=1; bridges[la]+=1; graph[lh].add(la); graph[la].add(lh)
        draws+=actual==.5; games+=1
    h,a=target['home_team_id'],target['away_team_id']; lh,la=membership.get(h),membership.get(a)
    reasons=[]
    for team,side,l in ((h,'home',lh),(a,'away',la)):
        if counts[team]<MIN_TEAM: reasons.append(side+': domestic_team_below_5')
        if venues[team,side]<MIN_SPLIT: reasons.append(side+': domestic_venue_below_3')
        if league_counts[l]<MIN_LEAGUE: reasons.append(side+': domestic_league_below_20')
        if bridges[l]<MIN_LEAGUE: reasons.append(side+': cross_league_bridge_below_20')
    seen={lh}; pending=[lh]
    while pending:
        for neighbor in sorted(graph[pending.pop()]):
            if neighbor not in seen: seen.add(neighbor); pending.append(neighbor)
    if la not in seen: reasons.append('unconnected_leagues')
    if reasons: return dict(status='insufficient_data', reasons=reasons, model_version=VERSION)
    gap=max(-1200,min(1200,ratings[h]-ratings[a]+offsets[lh]-offsets[la]+60))
    strength=10**(gap/400)
    draw_rate=(draws+5)/(games+20)
    draw_weight=2*draw_rate/(1-draw_rate)*math.sqrt(strength)
    denominator=strength+1+draw_weight
    return dict(status='ok', model_version=VERSION, home_probability=strength/denominator,
                draw_probability=draw_weight/denominator, away_probability=1/denominator,
                league_offset_home=offsets[lh], league_offset_away=offsets[la])


def metrics(rows):
    if not rows: return dict(matches=0,brier=None,log_loss=None,calibration=[])
    brier=[]; loss=[]; bins=defaultdict(list)
    for p,y in rows:
        brier.append(sum((v-int(i==y))**2 for i,v in enumerate(p)))
        loss.append(-math.log(max(1e-15,p[y])))
        for i,v in enumerate(p): bins[min(9,int(v*10))].append((v,int(i==y)))
    return dict(matches=len(rows),brier=mean(brier),log_loss=mean(loss),calibration=[
        dict(lower=k/10,upper=(k+1)/10,predictions=len(v),expected=mean(p for p,y in v),actual=mean(y for p,y in v))
        for k,v in sorted(bins.items())])


def report(source):
    now=stamp(source['as_of']); leagues={l['id']:l for l in source['leagues']}; teams={t['id']:t for t in source['teams']}
    rows=[{**r, **{k:stamp(r.get(k)) for k in ('kickoff_at','created_at','updated_at','last_scraped_at')}} for r in source['matches']]
    future=ordered(r for r in rows if leagues[r['league_id']]['external_id']==113
        and r['status']=='scheduled' and now<=stamp(r['kickoff_at'])<now+timedelta(days=30))
    present=[r for r in rows if usable(r,now)]; fixtures=[]
    for target in future:
        base=baseline(present,target); alt=challenger(present,target,leagues)
        sides={side:team_coverage(present,target,target[side+'_team_id'],side,leagues) for side in ('home','away')}
        for side in sides: sides[side].update(name=teams[target[side+'_team_id']]['name'],goaloo_team_id=teams[target[side+'_team_id']]['external_id'])
        fixtures.append(dict(match_id=target['id'],goaloo_match_id=target['external_match_id'],kickoff_at=target['kickoff_at'].isoformat(),
            **sides,current_status=base['status'],candidate_status=alt['status'],candidate_reasons=alt.get('reasons',[])))
    evaluated=[]; current=[]; candidate=[]; paired_current=[]; paired_candidate=[]
    targets=ordered(r for r in rows if valid(r) and leagues[r['league_id']]['external_id']==113 and stamp(r['kickoff_at'])<=now-timedelta(hours=3))
    for target in targets:
        cutoff=stamp(target['kickoff_at']); history=[r for r in rows if r['id']!=target['id'] and usable(r,cutoff)]
        b=baseline(history,target); c=challenger(history,target,leagues)
        y=0 if target['ft_home']>target['ft_away'] else 1 if target['ft_home']==target['ft_away'] else 2
        def scored(p): return ([p[k+'_probability'] for k in ('home','draw','away')],y)
        if b['status']=='ok': current.append(scored(b))
        if c['status']=='ok': candidate.append(scored(c))
        if b['status']==c['status']=='ok': paired_current.append(scored(b)); paired_candidate.append(scored(c))
        evaluated.append(dict(match_id=target['id'],year=cutoff.year,current=b['status'],candidate=c['status']))
    return dict(as_of=source['as_of'],model_version=VERSION,live_model_changed=False,
        fixtures=fixtures,upcoming_matches=len(fixtures),current_analyzable=sum(f['current_status']=='ok' for f in fixtures),
        candidate_analyzable=sum(f['candidate_status']=='ok' for f in fixtures),actual_after_analyzable=sum(f['current_status']=='ok' for f in fixtures),
        backtest=dict(period='2024–2026',policy='strict observed-before-kickoff; three-hour result buffer; no retrospective availability',
            targets=len(targets),current=metrics(current),candidate=metrics(candidate),paired_current=metrics(paired_current),paired_candidate=metrics(paired_candidate),
            evaluated=evaluated,paired_matches=len(paired_current),minimum_validation_matches=200,
            uncertainty='insufficient independent matches; no reliable confidence interval or superiority claim',
            years=[dict(year=year,targets=sum(r['year']==year for r in evaluated),
                current=sum(r['year']==year and r['current']=='ok' for r in evaluated),
                candidate=sum(r['year']==year and r['candidate']=='ok' for r in evaluated)) for year in (2024,2025,2026)],
            promotion='rejected: no independent prospective holdout and no reliable paired improvement established'))
