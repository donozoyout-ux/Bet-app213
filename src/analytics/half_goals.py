"""Independent first-half and second-half goal analytics.

Calculated from verified match score fields:
  ht_home, ht_away, ft_home, ft_away.
First-half goals = ht_home + ht_away.
Second-half home goals = ft_home - ht_home.
Second-half away goals = ft_away - ht_away.
Rejects missing, inconsistent, or negative scores without inventing data.
"""
import math
from src.analytics.match_statistics import poisson_over


def is_valid_half_match(match):
    """Verify that match has non-negative, consistent halftime and fulltime scores."""
    if match is None or getattr(match, 'status', None) != 'finished':
        return False
    ht_h = getattr(match, 'ht_home', None)
    ht_a = getattr(match, 'ht_away', None)
    ft_h = getattr(match, 'ft_home', None)
    ft_a = getattr(match, 'ft_away', None)
    if ht_h is None or ht_a is None or ft_h is None or ft_a is None:
        return False
    if ht_h < 0 or ht_a < 0 or ft_h < 0 or ft_a < 0:
        return False
    if ft_h < ht_h or ft_a < ht_a:
        # Impossible score combination: fulltime goals cannot be less than halftime
        return False
    return True


def get_half_scores(match, half: str):
    """Return (home_goals, away_goals) for the specified half ('first' or 'second')."""
    if not is_valid_half_match(match):
        return None, None
    if half == 'first':
        return match.ht_home, match.ht_away
    elif half == 'second':
        return match.ft_home - match.ht_home, match.ft_away - match.ht_away
    else:
        raise ValueError(f"Unknown half: {half}")


def extract_match(r):
    """Unpack Match instance from SQLAlchemy Row or 2-tuple (Match, MatchStatistics)."""
    return r[0] if hasattr(r, '__getitem__') and not hasattr(r, '__table__') else r


def half_goal_summary(rows, team_id: int, half: str):
    """Calculate verified half-goal performance for a team over a set of matches."""
    matches_considered = len(rows)
    scored_list = []
    conceded_list = []
    total_list = []
    missing_count = 0
    missing_reason = None

    for r in rows:
        m = extract_match(r)
        if getattr(m, 'status', None) != 'finished':
            continue
        h, a = get_half_scores(m, half)
        if h is None or a is None:
            missing_count += 1
            if missing_reason is None:
                ht_h = getattr(m, 'ht_home', None)
                ht_a = getattr(m, 'ht_away', None)
                ft_h = getattr(m, 'ft_home', None)
                ft_a = getattr(m, 'ft_away', None)
                if ht_h is None or ht_a is None or ft_h is None or ft_a is None:
                    missing_reason = 'missing_scores'
                elif ht_h < 0 or ht_a < 0 or ft_h < 0 or ft_a < 0:
                    missing_reason = 'negative_scores'
                elif ft_h < ht_h or ft_a < ht_a:
                    missing_reason = 'inconsistent_scores'
                else:
                    missing_reason = 'incomplete_half_data'
            continue

        is_home = (m.home_team_id == team_id)
        gf, ga = (h, a) if is_home else (a, h)
        scored_list.append(gf)
        conceded_list.append(ga)
        total_list.append(gf + ga)

    sample_size = len(total_list)
    avg = lambda vals: sum(vals) / len(vals) if vals else None
    thresholds = [0.5, 1.5, 2.5]
    over_rates = {f'over_{str(t).replace(".", "_")}': sum(v > t for v in total_list) / sample_size if sample_size else None for t in thresholds}
    under_rates = {f'under_{str(t).replace(".", "_")}': sum(v < t for v in total_list) / sample_size if sample_size else None for t in thresholds}

    return {
        'matches_considered': matches_considered,
        'sample_size': sample_size,
        'for_sample_size': sample_size,
        'against_sample_size': sample_size,
        'for_avg': avg(scored_list),
        'against_avg': avg(conceded_list),
        'total_avg': avg(total_list),
        'over_rates': over_rates,
        'under_rates': under_rates,
        'missing_total_sample_size': missing_count,
        'missing_data_reason': missing_reason if missing_count else None,
        'rate_basis': f'{half}_half_goals'
    }


def half_goal_prediction(rows, home_id: int, away_id: int, half: str):
    """Compute Poisson expectations and probabilities for first or second half goals."""
    complete = []
    for r in rows:
        m = extract_match(r)
        if getattr(m, 'status', None) != 'finished':
            continue
        h, a = get_half_scores(m, half)
        if h is not None and a is not None:
            complete.append((m, h, a))

    home = [r for r in complete if home_id in (r[0].home_team_id, r[0].away_team_id)][:10]
    away = [r for r in complete if away_id in (r[0].home_team_id, r[0].away_team_id)][:10]
    hs = [r for r in complete if r[0].home_team_id == home_id][:10]
    aws = [r for r in complete if r[0].away_team_id == away_id][:10]

    used = {r[0].id for r in home + away + hs + aws}
    result = {
        'status': 'insufficient_data',
        'sample_size': len(used),
        'league_sample_size': min(200, len(complete)),
        'home_sample_size': len(home),
        'away_sample_size': len(away),
        'home_split_sample_size': len(hs),
        'away_split_sample_size': len(aws),
        'basis': f'{half}_half_goals',
        'expected_home': None,
        'expected_away': None,
        'expected_total': None,
        'over_probabilities': {},
        'under_probabilities': {}
    }
    required = {
        'league_sample_size': 20,
        'home_sample_size': 5,
        'away_sample_size': 5,
        'home_split_sample_size': 3,
        'away_split_sample_size': 3
    }
    result['required_samples'] = required
    result['insufficient_reasons'] = [
        {'code': key, 'actual': result[key], 'required': minimum}
        for key, minimum in required.items() if result[key] < minimum
    ]
    if result['insufficient_reasons']:
        return result

    baseline = complete[:200]
    bh = sum(r[1] for r in baseline) / len(baseline)
    ba = sum(r[2] for r in baseline) / len(baseline)
    if bh == 0 or ba == 0:
        result['insufficient_reasons'] = [{'code': 'zero_league_baseline'}]
        return result

    shrink = lambda total, n, prior: (total + 5 * prior) / (n + 5)
    expected_home = shrink(sum(r[1] for r in hs), len(hs), bh) * shrink(sum(r[1] for r in aws), len(aws), bh) / bh
    expected_away = shrink(sum(r[2] for r in aws), len(aws), ba) * shrink(sum(r[2] for r in hs), len(hs), ba) / ba
    expected_home, expected_away = min(8.0, max(0.0, expected_home)), min(8.0, max(0.0, expected_away))
    expected_total = expected_home + expected_away

    thresholds = [0.5, 1.5, 2.5]
    over_probs = {str(t): poisson_over(expected_total, t) for t in thresholds}
    under_probs = {str(t): (1.0 - over_probs[str(t)]) if over_probs[str(t)] is not None else None for t in thresholds}

    result.update(
        status='ok',
        expected_home=expected_home,
        expected_away=expected_away,
        expected_total=expected_total,
        over_probabilities=over_probs,
        under_probabilities=under_probs
    )
    return result
