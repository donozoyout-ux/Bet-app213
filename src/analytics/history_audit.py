"""Read-only audit evaluator. Never invokes capture, settlement writes or scrapers.

Rates describe recorded selections against database evidence, not independently
verified source truth. Correction plans are deterministic proposals, not commands.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import re

TERMINAL = {'won', 'lost', 'push', 'void', 'half_won', 'half_lost'}
BOOKS = {'Crown', 'Bet365', 'Sbobet'}


def stamp(value):
    if not value:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate(match, stats, pick):
    """Independent arithmetic; unsupported evidence stays unknown, never a loss."""
    market, selection = pick.get('market'), pick.get('selection')
    if not isinstance(market, str) or not isinstance(selection, str):
        return 'awaiting_data', None, 'unsupported_selection'
    totals = {'goals', 'asian_totals', 'corners', 'cards', 'yellow_cards',
              'red_cards', 'first_half_goals', 'second_half_goals'}
    choices = {'result': {'home', 'draw', 'away'}, 'btts': {'yes', 'no'},
               'double_chance': {'1x', 'x2', '12'}}
    pattern = (r'(over|under)_(\d+)_(0|5|25|75)' if market in totals else
               r'(home|away)_(plus|minus)_(\d+)_(0|5|25|75)' if market == 'asian_handicap' else None)
    parsed = re.fullmatch(pattern, selection) if pattern and isinstance(selection, str) else None
    if not parsed and selection not in choices.get(market, set()):
        return 'awaiting_data', None, 'unsupported_selection'
    if not match:
        return 'awaiting_data', None, 'missing_match'
    if match['status'] in {'cancelled', 'abandoned'}:
        return 'void', None, 'application_void_policy; bookmaker_rules_not_verified'
    if match['status'] != 'finished':
        return 'pending', None, 'not_final'
    stats = stats or {}
    if market in {'corners', 'cards', 'yellow_cards', 'red_cards'}:
        fields = [f'{side}_{kind}' for side in ('home', 'away') for kind in
                  (('yellow_cards', 'red_cards') if market == 'cards' else (market,))]
        values = [stats.get(f) for f in fields]
        if not stats.get('is_final') or any(not number(v) or v < 0 or int(v) != v for v in values):
            return 'awaiting_data', None, 'missing_or_nonfinal_count_evidence'
        total = sum(values)
    else:
        home, away = match.get('ft_home'), match.get('ft_away')
        half = [match.get('ht_home'), match.get('ht_away')]
        needed = half if market == 'first_half_goals' else [home, away] + (half if market == 'second_half_goals' else [])
        if any(not number(v) or v < 0 or int(v) != v for v in needed):
            return 'awaiting_data', None, 'missing_or_invalid_score'
        if all(number(v) for v in [home, away, *half]) and (home < half[0] or away < half[1]):
            return 'awaiting_data', None, 'inconsistent_half_full_score'
        total = sum(half) if market == 'first_half_goals' else home+away
        if market == 'second_half_goals':
            total -= sum(half)
    if market in choices:
        won = ({'home': home > away, 'draw': home == away, 'away': home < away}[selection] if market == 'result' else
               ((home > 0 and away > 0) == (selection == 'yes')) if market == 'btts' else
               {'1x': home >= away, 'x2': home <= away, '12': home != away}[selection])
        return ('won' if won else 'lost'), f'{home}-{away}', 'final_score'
    groups = parsed.groups()
    line = float(groups[-2]+'.'+groups[-1])
    if market == 'asian_handicap':
        line *= 1 if groups[1] == 'plus' else -1
        value = home-away if groups[0] == 'home' else away-home
        difference = lambda part: value+part
        actual = f'{home}-{away}'
    else:
        difference = lambda part: (total-part) * (1 if groups[0] == 'over' else -1)
        actual = total
    # Quarter lines split stakes over adjacent half-goal lines.
    if (line*2) % 1:
        if market not in {'asian_handicap', 'asian_totals'}:
            return 'awaiting_data', actual, 'quarter_line_requires_asian_contract'
        parts = [math.floor(line*2)/2, math.ceil(line*2)/2]
    else:
        parts = [line]
    results = [0 if difference(p) == 0 else 1 if difference(p) > 0 else -1 for p in parts]
    value = sum(results)/len(results)
    return {1:'won', -1:'lost', 0:'push', .5:'half_won', -.5:'half_lost'}[value], actual, 'final_evidence'


def price_at_capture(pick, captured, quote=None):
    vo = pick.get('verified_odds') or {}
    q = quote if quote is not None else dict(price=vo.get('best_price'), bookmaker=vo.get('best_bookmaker'),
        observed_at=vo.get('observed_at'), expires_at=vo.get('expires_at'))
    try:
        seen, expiry = stamp(q.get('observed_at')), stamp(q.get('expires_at'))
        valid = (number(q.get('price')) and q['price'] > 1 and q.get('bookmaker') in BOOKS
            and seen is not None and captured is not None and 0 <= (captured-seen).total_seconds() <= 900
            and expiry is not None and captured < expiry
            and (quote is not None or vo.get('has_odds') is True and vo.get('stage') == 'latest'))
    except (ValueError, TypeError):
        valid = False
    return q.get('bookmaker') or 'unknown', q.get('price') if valid else None


def summarize(rows):
    counts = Counter(r['expected'] for r in rows)
    recorded = Counter(r.get('stored') for r in rows if r['eligible_for_rate'] and r.get('stored') in {'won','lost','void'})
    recorded_n = recorded['won']+recorded['lost']
    binary = [r for r in rows if r['expected'] in {'won', 'lost'} and r['eligible_for_rate']]
    won = sum(r['expected'] == 'won' for r in binary)
    n = len(binary)
    calibrated = [r for r in binary if number(r['probability']) and 0 <= r['probability'] <= 1]
    priced = [r for r in rows if r['eligible_for_rate'] and r['price'] is not None
              and r['expected'] in TERMINAL-{'void'}]
    profit = sum({'won':r['price']-1, 'lost':-1, 'push':0,
                  'half_won':(r['price']-1)/2, 'half_lost':-.5}[r['expected']] for r in priced)
    interval = None
    if n:
        p, z = won/n, 1.96
        center = (p+z*z/(2*n))/(1+z*z/n)
        radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        interval = [center-radius, center+radius]
    return dict(selections=len(rows), outcomes=dict(counts), denominator=n, wins=won,
        recorded_outcomes=dict(recorded), recorded_denominator=recorded_n,
        recorded_hit_rate=recorded['won']/recorded_n if recorded_n else None,
        hit_rate=won/n if n else None, wilson_95=interval,
        calibrated_n=len(calibrated), brier=sum((r['probability']-(r['expected']=='won'))**2 for r in calibrated)/len(calibrated) if calibrated else None,
        mean_probability=sum(r['probability'] for r in calibrated)/len(calibrated) if calibrated else None,
        priced_n=len(priced), unit_stake_profit=profit, unit_stake_roi=profit/len(priced) if priced else None)


def audit(snapshots, coupons=(), coupon_matches=(), *, observed_at):
    rows, issues = [], []
    def append(source, key, match, stats, pick, captured, league, stored=None, quote=None, extra=()):
        expected, actual, reason = evaluate(match, stats, pick)
        when, kickoff = stamp(captured), stamp(match.get('kickoff_at')) if match else None
        eligible = bool(when and kickoff and when < kickoff and 'integrity_mismatch' not in extra)
        bookmaker, price = price_at_capture(pick, when, quote)
        row = dict(source=source, key=key, match_id=match.get('id') if match else None,
            external_match_id=match.get('external_match_id') if match else None, league=league,
            month=kickoff.strftime('%Y-%m') if kickoff else 'unknown', kickoff_at=kickoff.isoformat() if kickoff else None,
            captured_at=when.isoformat() if when else None, market=pick.get('market'), selection=pick.get('selection'),
            confidence=pick.get('confidence','unknown'), probability=pick.get('probability'), bookmaker=bookmaker,
            price=price, expected=expected, actual=actual, stored=stored, reason=reason, eligible_for_rate=eligible,
            evidence=dict(match=match, stats=stats))
        row.update({field:match.get(field) if match else None for field in ('ft_home','ft_away','ht_home','ht_away')})
        flags = list(extra)
        if not (when and kickoff and when < kickoff): flags.append('not_verifiable_prematch')
        normalized = 'void' if expected == 'push' else expected
        if stored is None and expected in TERMINAL and source == 'prediction': flags.append('missing_terminal_decision')
        elif stored is not None and stored != normalized and not (source == 'coupon' and stored == 'pending'):
            flags.append('stored_vs_current_evidence_disagreement')
        if source == 'coupon':
            row['status_semantics'] = 'immutable_capture_status; live settlement is derived by coupon detail API'
        if expected == 'awaiting_data': flags.append(reason)
        if match and expected == 'pending' and kickoff and (stamp(observed_at)-kickoff).total_seconds()>21600:
            flags.append('overdue_unfinished_fixture')
        row['flags'] = sorted(set(flags));rows.append(row)
        for flag in row['flags']:
            proposal = dict(source=source, key=key, match_id=row['match_id'], flag=flag,
                expected=expected, stored=stored, actual=actual,
                action='review_source_then_append_revision_in_separately_approved_change',
                evidence_digest=digest(row['evidence']))
            proposal['plan_id'] = digest(proposal)
            issues.append(proposal)
    for snapshot in snapshots:
        latest = {}
        for d in snapshot['decisions']:
            if d['pick_index'] not in latest or d['revision']>latest[d['pick_index']]['revision']:
                latest[d['pick_index']] = d
        for index, pick in enumerate(snapshot['picks']):
            d, flags = latest.get(index), []
            if d:
                snapshot_proof = {k:snapshot[k] for k in ('match_id','league_id','model_version','picks')}
                snapshot_proof['captured_at'] = stamp(snapshot['captured_at']).isoformat()
                if digest(snapshot_proof)!=d['snapshot_hash'] or digest(d['evidence'])!=d['evidence_hash']:
                    flags.append('integrity_mismatch')
                proof = d['evidence']
                stats = dict(proof.get('statistics') or {}, is_final=proof.get('is_final'))
                outcome, actual, _ = evaluate(proof, stats, pick)
                if ('void' if outcome=='push' else outcome, actual)!=(d['outcome'],d['actual'].get('value')):
                    flags.append('decision_disagrees_with_frozen_evidence')
            append('prediction',f"{snapshot['match_id']}:{index}",snapshot['match'],snapshot['stats'],pick,
                snapshot['captured_at'],snapshot['league'],d['outcome'] if d else None,extra=flags)
            if d:
                rows[-1]['recorded_decision'] = {key:d[key] for key in ('id','revision','outcome','actual',
                    'evidence','snapshot_hash','evidence_hash','recorded_at','evaluator_version')}
                for proposal in issues:
                    if proposal['source']=='prediction' and proposal['key']==rows[-1]['key']:
                        proposal.update(expected_revision=d['revision'],snapshot_hash=d['snapshot_hash'],
                            recorded_evidence_hash=d['evidence_hash'])
                        proposal.pop('plan_id',None)
                        proposal['plan_id']=digest(proposal)
        for index in latest.keys()-set(range(len(snapshot['picks']))):
            issues.append(dict(source='prediction',key=f"{snapshot['match_id']}:{index}",flag='orphan_pick_index'))
    matches = {m['id']:m for m in coupon_matches}
    for coupon in coupons:
        # Never publish coupon IDs/root IDs or owner hashes in the report.
        public = hashlib.sha256(coupon['id'].encode()).hexdigest()[:16]
        for index, item in enumerate(coupon.get('items') or []):
            m = matches.get(item['match']['id'])
            match = {k:v for k,v in m.items() if k not in {'stats','league'}} if m else None
            append('coupon',f'{public}:{index}',match,m.get('stats') if m else None,item['recommendation'],
                coupon['created_at'],m['league'] if m else 'unknown',item.get('status'),item.get('quote'))
    groups = {}
    for source in ('prediction','coupon'):
        selected = [r for r in rows if r['source']==source]
        groups[source] = {'overall':summarize(selected)}
        paired = defaultdict(list)
        for row in selected:paired[row['league']+' | '+str(row['market'])].append(row)
        groups[source]['league_market'] = {key:summarize(value) for key,value in sorted(paired.items())}
        for dimension in ('league','market','month','confidence','bookmaker'):
            buckets = defaultdict(list)
            for row in selected:buckets[str(row[dimension])].append(row)
            groups[source][dimension] = {key:summarize(value) for key,value in sorted(buckets.items())}
    return dict(observed_at=observed_at, method='readonly-history-audit-v1', dry_run=True,
        limitations=['Database evidence only; no independent Goaloo source verification.',
            'Coupon rows count immutable versions, not independent wagers; no actual stakes recorded.',
            'ROI is a one-unit simulation only over valid captured prices, never actual realized profit.',
            'Wilson intervals assume independent selections; same-match/version correlation weakens this assumption.',
            'Pushes are separated from application voids; bookmaker abandonment rules are not independently verified.'],
        groups=groups, rows=rows, discrepancies=issues)
