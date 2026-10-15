"""Offline candidates only: fixed temporal train/validation/test with as-of gates."""
from collections import Counter
import math
from src.analytics.learning_metrics import clip, losses, metrics, effective_matches, EPSILON

MIN_TRAIN, MIN_VALIDATION, MIN_TEST = 200, 100, 100


def temporal_split(rows):
    # Whole UTC match dates stay together, regardless of pick count or league.
    dates = sorted({r['kickoff_at'].date() for r in rows})
    if len(dates) < 3:
        return [], [], [], dict(reason='insufficient_dates')
    first = max(1, min(len(dates)-2, int(len(dates)*.6)))
    second = max(first+1, min(len(dates)-1, int(len(dates)*.8)))
    validation_date, test_date = dates[first], dates[second]
    validation_start = min(r['kickoff_at'] for r in rows if r['kickoff_at'].date() >= validation_date)
    test_start = min(r['kickoff_at'] for r in rows if r['kickoff_at'].date() >= test_date)
    train = [r for r in rows if r['kickoff_at'].date() < validation_date and r['available_at'] < validation_start]
    validation = [r for r in rows if validation_date <= r['kickoff_at'].date() < test_date and r['available_at'] < test_start]
    test = [r for r in rows if r['kickoff_at'].date() >= test_date]
    return train, validation, test, dict(validation_start=validation_start.isoformat(),
        test_start=test_start.isoformat(), excluded_late_outcomes=len(rows)-len(train)-len(validation)-len(test))


def transform(p, slope, intercept):
    q = clip(p)
    z = max(-30, min(30, slope*math.log(q/(1-q))+intercept))
    return clip(1/(1+math.exp(-z)))


def fit(rows, *, slope_enabled):
    # Fixed optimizer/regularization, no tuning on held-out test results.
    counts = Counter(r['match_id'] for r in rows)
    prepared = [(math.log(clip(r['probability'])/(1-clip(r['probability']))), r['y'], 1/counts[r['match_id']]) for r in rows]
    denominator = len(counts)
    slope, intercept = 1., 0.
    for _ in range(200):
        da = db = 0.
        for x, y, weight in prepared:
            z = max(-30, min(30, slope*x+intercept))
            error = (1/(1+math.exp(-z))-y)*weight
            da += error*x
            db += error
        if slope_enabled:
            slope = max(.05, min(5., slope-.2*(da/denominator+.01*(slope-1))))
        intercept = max(-5., min(5., intercept-.2*(db/denominator+.01*intercept)))
    return dict(slope=slope, intercept=intercept)


def predictions(rows, parameters):
    return [dict(r, probability=transform(r['probability'], **parameters)) for r in rows]


def paired_bounds(rows, calibrated, *, families=1):
    # Two prespecified scores and all tested model versions share family alpha .05.
    alpha = .05/(2*families)
    n = len(rows)
    neff = effective_matches(rows)
    radius = math.sqrt(2*math.log(1/alpha)/neff)
    original = [losses(r['probability'],r['y']) for r in rows]
    candidate = [losses(r['probability'],r['y']) for r in calibrated]
    brier = sum(b[0]-a[0] for a,b in zip(original,candidate))/n
    logloss = sum(b[1]-a[1] for a,b in zip(original,candidate))/n
    return dict(brier_delta=brier, log_loss_delta=logloss,
        brier_delta_upper=brier+radius,
        log_loss_delta_upper=logloss+(-math.log(EPSILON))*radius,
        alpha_per_score=alpha, method='one_sided_paired_match_cluster_hoeffding', effective_matches=neff)


def evaluate_calibration(rows, *, families=1):
    minimums = dict(train=MIN_TRAIN, validation=MIN_VALIDATION, test=MIN_TEST)
    base = dict(status='insufficient_sample', automatic_model_change=False,
        minimum_matches=minimums, candidates_researched=['logit_offset','regularized_logit_sigmoid'],
        isotonic='deferred: flexible stepwise fits need more data', fitted=False)
    if len({r['model_version'] for r in rows})>1:
        return dict(base,reason='mixed_model_versions')
    # Never train on small cohorts, even if many correlated picks are present.
    if len({r['match_id'] for r in rows}) < sum(minimums.values()):
        return dict(base, reason='insufficient_matches', matches=len({r['match_id'] for r in rows}))
    train, validation, test, boundaries = temporal_split(rows)
    partitions = dict(train=train, validation=validation, test=test)
    counts = {name:len({r['match_id'] for r in subset}) for name,subset in partitions.items()}
    base.update(split=boundaries, matches=counts)
    if any(counts[name] < minimums[name] or effective_matches(subset) < minimums[name]
           for name,subset in partitions.items()):
        return dict(base, reason='insufficient_time_eligible_matches')
    if min(len({r['match_id'] for r in train if r['y']==y}) for y in (0,1)) < 20:
        return dict(base, reason='insufficient_training_classes')
    validation_baseline = metrics(validation, bins=False)
    candidates = []
    for name in base['candidates_researched']:
        parameters = fit(train, slope_enabled=name=='regularized_logit_sigmoid')
        scores = metrics(predictions(validation, parameters), bins=False)
        candidates.append(dict(name=name, parameters=parameters, validation=scores))
    eligible = [c for c in candidates if c['validation']['brier_score'] < validation_baseline['brier_score']
                and c['validation']['log_loss'] < validation_baseline['log_loss']]
    base.update(fitted=True, candidates=candidates, validation_baseline=validation_baseline)
    if not eligible:
        return dict(base, status='rejected', reason='no_validation_improvement')
    chosen = min(eligible, key=lambda c:(c['validation']['brier_score'],c['name']))
    calibrated = predictions(test,chosen['parameters'])
    evidence = paired_bounds(test,calibrated,families=families)
    accepted = evidence['brier_delta_upper'] < 0 and evidence['log_loss_delta_upper'] < 0
    return dict(base, status='offline_candidate' if accepted else 'rejected',
        reason='reliable_heldout_improvement' if accepted else 'heldout_improvement_not_reliable',
        chosen=chosen['name'], parameters=chosen['parameters'], test_baseline=metrics(test,bins=False),
        test_candidate=metrics(calibrated,bins=False), comparison=evidence)
