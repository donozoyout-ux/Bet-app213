"""Deterministic, descriptive scores and conservative match-cluster uncertainty.

No live probability or selection code imports this module. Bounds assume matches
are independent, allow arbitrary dependence between picks within a match, and
do not assert independence of repeated teams across matches.
"""
from collections import Counter, defaultdict
import math
from decimal import Context, Decimal, localcontext
from functools import lru_cache

METHOD = 'learning-v2-cluster-hoeffding-temporal'
EPSILON = 1e-6
MIN_LESSON_MATCHES = 100


def clip(p):
    return min(1-EPSILON, max(EPSILON, p))


@lru_cache(maxsize=8192)
def stable_log(value):
    with localcontext(Context(prec=40)):
        return float(Decimal.from_float(float(value)).ln())


@lru_cache(maxsize=8192)
def stable_sqrt(value):
    with localcontext(Context(prec=40)):
        return float(Decimal.from_float(float(value)).sqrt())


@lru_cache(maxsize=8192)
def stable_sigmoid(value):
    with localcontext(Context(prec=40)):
        z=Decimal.from_float(float(value))
        return float(Decimal(1)/(Decimal(1)+(-z).exp()))


@lru_cache(maxsize=8192)
def losses(p, y):
    # Decimal.ln is correctly rounded independently of the platform libm.
    # Preserve the exact input float, including log1p's 1-q precision; do not
    # round stored probabilities or weaken exact report comparisons.
    with localcontext(Context(prec=40)):
        q = Decimal.from_float(float(clip(p)))
        log_loss = float(-(q if y else Decimal(1)-q).ln())
    difference = p-y
    return difference*difference, log_loss


def effective_matches(rows):
    counts = Counter(r['match_id'] for r in rows)
    return len(rows)**2/sum(n*n for n in counts.values()) if counts else 0


def metrics(rows, *, alpha=.05, bins=True):
    n = len(rows)
    if not n:
        return dict(predictions=0, matches=0, effective_matches=0, success_rate=None,
                    mean_probability=None, observed_minus_expected=None, brier_score=None,
                    log_loss=None, success_interval=None, gap_interval=None, calibration=[])
    probability = math.fsum(r['probability'] for r in rows)/n
    success = sum(r['y'] for r in rows)/n
    neff = effective_matches(rows)
    radius = stable_sqrt(stable_log(2/alpha)/(2*neff))
    scores = [losses(r['probability'], r['y']) for r in rows]
    gap = success-probability
    result = dict(predictions=n, matches=len({r['match_id'] for r in rows}), effective_matches=neff,
        success_rate=success, mean_probability=probability, observed_minus_expected=gap,
        expected_wins=n*probability, observed_wins=sum(r['y'] for r in rows),
        brier_score=math.fsum(x[0] for x in scores)/n, log_loss=math.fsum(x[1] for x in scores)/n,
        success_interval=[max(0, success-radius), min(1, success+radius)],
        gap_interval=[max(-1, gap-2*radius), min(1, gap+2*radius)],
        confidence_level=1-alpha, uncertainty_method='weighted_match_cluster_hoeffding',
        uncertainty_assumption='independent_matches_not_independent_picks',
        calibration=[], log_loss_epsilon=EPSILON)
    if bins:
        buckets = defaultdict(list)
        for row in rows:
            buckets[min(int(row['probability']*10), 9)].append(row)
        for index, bucket in sorted(buckets.items()):
            # Simultaneous bounds for all ten prespecified bins.
            result['calibration'].append(dict(lower=index/10, upper=(index+1)/10,
                upper_inclusive=index==9, **metrics(bucket, alpha=alpha/10, bins=False)))
    return result


def normalize(items):
    """Filter settled voids and bad probabilities; never impute a missing p."""
    rows = []
    excluded = Counter()
    for item in items:
        if item['outcome'] not in {'won', 'lost'}:
            excluded[item['outcome']] += 1
            continue
        p = item['prediction'].get('probability')
        if isinstance(p, bool) or not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1:
            excluded['invalid_probability'] += 1
            continue
        rows.append(dict(match_id=item['match_id'], pick_index=item['pick_index'],
            league_id=item['league_id'], market=item['prediction']['market'],
            model_version=item['model_version'], probability=p, y=int(item['outcome']=='won'),
            kickoff_at=item['kickoff_at'], available_at=item['recorded_at'],
            revision=item['revision'], settlement_id=item['settlement_id']))
    rows.sort(key=lambda r:(r['kickoff_at'],r['match_id'],r['pick_index'],r['settlement_id']))
    return rows, dict(sorted(excluded.items()))


def analyze(items):
    """Linear grouping; no model fit until explicit per-version eligibility gates."""
    from src.analytics.learning_calibration import evaluate_calibration
    rows, excluded = normalize(items)
    scopes = defaultdict(list)
    for row in rows:
        for kind, key in (('league', str(row['league_id'])), ('market', row['market']),
                          ('model', row['model_version']),
                          ('league_market', f"{row['league_id']}:{row['market']}")):
            scopes[kind, key].append(row)
    slices, lessons = [], []
    # Bonferroni covers all searched lesson scopes, including overlapping groups.
    tests = max(1, len(scopes)+1)
    for (kind, key), subset in [(('overall', 'all'), rows), *sorted(scopes.items())]:
        measured = metrics(subset, alpha=.05/tests)
        if kind != 'overall':
            slices.append(dict(kind=kind, scope_key=key, payload=measured))
        sufficient = measured['matches'] >= MIN_LESSON_MATCHES and measured['effective_matches'] >= MIN_LESSON_MATCHES
        gap = measured['gap_interval']
        supported = sufficient and (gap[0] > 0 or gap[1] < 0)
        status = 'supported_candidate' if supported else 'inconclusive' if sufficient else 'insufficient_sample'
        league = subset[0]['league_id'] if kind in {'league', 'league_market'} and subset else None
        market = subset[0]['market'] if kind in {'market', 'league_market'} and subset else None
        lessons.append(dict(scope_key=f'{kind}:{key}', payload=dict(
            title='Örneklem yetersiz' if not sufficient else 'Kalibrasyon sapması adayı' if supported else 'Belirgin sapma bulunamadı',
            league_id=league, market=market, model_version=key if kind=='model' else None,
            predictions=measured['predictions'], matches=measured['matches'],
            expected_success=measured['mean_probability'], observed_success=measured['success_rate'],
            status=status, confidence='moderate' if supported else 'low',
            evidence=dict(method_version=METHOD, gap_interval=gap, family_tests=tests,
                family_alpha=.05, effective_matches=measured['effective_matches'],
                minimum_effective_matches=MIN_LESSON_MATCHES,
                calculation='mean(y-p); weighted match-cluster Hoeffding; Bonferroni scopes',
                assumption='independent matches; repeated-team and time dependence may remain'),
            automatic_model_change=False)))
    for (kind, key), subset in sorted(scopes.items()):
        if kind == 'model':
            slices.append(dict(kind='calibration', scope_key=key, payload=evaluate_calibration(subset, families=sum(kind=='model' for kind,_ in scopes))))
    return dict(summary=metrics(rows), excluded=excluded, slices=slices, lessons=lessons)
