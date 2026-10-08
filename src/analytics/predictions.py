"""Independent Poisson goals; venue scoring rates shrunk toward league averages.

This is a statistical estimate, not measured xG or an ML model. Minimums: 20
competition results, 5 recent results per team and 3 results in each venue split.
Five baseline-weight units regularize scoring rates; no synthetic matches enter
the reported form, rates or sample count.
"""
from dataclasses import dataclass
from datetime import datetime
import math

MIN_LEAGUE = 20
MIN_TEAM = 5
MIN_SPLIT = 3
PRIOR_WEIGHT = 5


@dataclass(frozen=True)
class Result:
    id: int
    home_id: int
    away_id: int
    home_goals: int
    away_goals: int
    kickoff_at: datetime
    home: str = ''
    away: str = ''


def team_stats(results, team_id):
    matches = []
    for row in results:
        if team_id not in (row.home_id, row.away_id):
            continue
        home = row.home_id == team_id
        gf, ga = (row.home_goals, row.away_goals) if home else (row.away_goals, row.home_goals)
        matches.append({'id': row.id, 'date': row.kickoff_at, 'home': row.home, 'away': row.away,
                        'home_goals': row.home_goals, 'away_goals': row.away_goals,
                        'goals_for': gf, 'goals_against': ga, 'result': 'G' if gf > ga else 'B' if gf == ga else 'M'})
    n = len(matches)
    gf, ga = sum(row['goals_for'] for row in matches), sum(row['goals_against'] for row in matches)
    rate = lambda condition: sum(condition(row) for row in matches) / n if n else None
    return {'matches': matches, 'sample_size': n, 'form': [row['result'] for row in matches],
            'wins': sum(row['result'] == 'G' for row in matches), 'draws': sum(row['result'] == 'B' for row in matches),
            'losses': sum(row['result'] == 'M' for row in matches), 'goals_for': gf, 'goals_against': ga,
            'avg_goals_for': gf / n if n else None, 'avg_goals_against': ga / n if n else None,
            'win_rate': rate(lambda row: row['result'] == 'G'), 'draw_rate': rate(lambda row: row['result'] == 'B'),
            'loss_rate': rate(lambda row: row['result'] == 'M'),
            'clean_sheet_rate': rate(lambda row: row['goals_against'] == 0),
            'btts_rate': rate(lambda row: row['goals_for'] > 0 and row['goals_against'] > 0),
            'over_15_rate': rate(lambda row: row['goals_for'] + row['goals_against'] > 1.5),
            'over_25_rate': rate(lambda row: row['goals_for'] + row['goals_against'] > 2.5),
            'over_35_rate': rate(lambda row: row['goals_for'] + row['goals_against'] > 3.5),
            'avg_total_goals': (gf + ga) / n if n else None}


def implied_probabilities(prices):
    values = [prices.get(key) for key in ('home', 'draw', 'away')]
    if any(value is None or not math.isfinite(value) or value <= 1 for value in values):
        return None
    inverse = [1 / value for value in values]
    total = sum(inverse)
    return {'home': inverse[0] / total, 'draw': inverse[1] / total, 'away': inverse[2] / total,
            'overround': total - 1}


def poisson_probabilities(home_goals, away_goals):
    def distribution(mean):
        if not math.isfinite(mean) or mean < 0 or mean > 8:
            raise ValueError('Expected goals outside supported model bounds')
        # At lambda <= 8, truncating at 40 leaves negligible tail probability.
        values = [math.exp(-mean)]
        for goals in range(1, 41):
            values.append(values[-1] * mean / goals)
        total = sum(values)
        return [value / total for value in values]
    home, away = distribution(home_goals), distribution(away_goals)
    win = draw = loss = over = btts = 0.0
    for h, ph in enumerate(home):
        for a, pa in enumerate(away):
            p = ph * pa
            if h > a: win += p
            elif h == a: draw += p
            else: loss += p
            if h + a > 2: over += p
            if h and a: btts += p
    return {'home_probability': win, 'draw_probability': draw, 'away_probability': loss,
            'over_25_probability': over, 'under_25_probability': 1 - over,
            'btts_probability': btts, 'no_btts_probability': 1 - btts}


def predict(history, home_id, away_id, competition_type, consensus=None):
    history = sorted(history, key=lambda row: (row.kickoff_at, row.id), reverse=True)
    baseline = history[:200]
    home_rows = [row for row in history if home_id in (row.home_id, row.away_id)][:10]
    away_rows = [row for row in history if away_id in (row.home_id, row.away_id)][:10]
    home_split = [row for row in history if row.home_id == home_id][:10]
    away_split = [row for row in history if row.away_id == away_id][:10]
    h5, a5 = team_stats(home_rows[:5], home_id), team_stats(away_rows[:5], away_id)
    h10, a10 = team_stats(home_rows, home_id), team_stats(away_rows, away_id)
    hs, aws = team_stats(home_split, home_id), team_stats(away_split, away_id)
    used = {row.id for row in home_rows + away_rows + home_split + away_split}
    base = {'status': 'insufficient_data', 'confidence': 'low', 'sample_size': len(used),
            'league_sample_size': len(baseline), 'home_sample_size': len(home_rows), 'away_sample_size': len(away_rows),
            'home_venue_sample_size': len(home_split), 'away_venue_sample_size': len(away_split),
            'model_version': 'venue-poisson-v1', 'confidence_rules': {}, 'model_market_difference': None}
    extra = {'home_form': h5, 'away_form': a5, 'home_last_10': h10, 'away_last_10': a10,
             'home_split': hs, 'away_split': aws}
    if len(baseline) < MIN_LEAGUE or min(len(home_rows), len(away_rows)) < MIN_TEAM or min(len(home_split), len(away_split)) < MIN_SPLIT:
        return base, extra
    bh = sum(row.home_goals for row in baseline) / len(baseline)
    ba = sum(row.away_goals for row in baseline) / len(baseline)
    if bh == 0 or ba == 0:
        # A zero scoring baseline cannot establish relative strength reliably.
        # Abstain instead of presenting a tiny-sample 100%/0% outcome as certain.
        return base, extra
    shrink = lambda total, n, prior: (total + PRIOR_WEIGHT * prior) / (n + PRIOR_WEIGHT)
    home_attack = shrink(hs['goals_for'], hs['sample_size'], bh)
    home_defense = shrink(hs['goals_against'], hs['sample_size'], ba)
    away_attack = shrink(aws['goals_for'], aws['sample_size'], ba)
    away_defense = shrink(aws['goals_against'], aws['sample_size'], bh)
    eh = home_attack * away_defense / bh if bh else (home_attack + away_defense) / 2
    ea = away_attack * home_defense / ba if ba else (away_attack + home_defense) / 2
    eh, ea = min(8, max(0, eh)), min(8, max(0, ea))
    probabilities = poisson_probabilities(eh, ea)
    difference = {key: probabilities[f'{key}_probability'] - consensus[key] for key in ('home','draw','away')} if consensus else None
    agreement = all(abs(five['avg_goals_for'] - ten['avg_goals_for']) <= .5 and abs(five['avg_goals_against'] - ten['avg_goals_against']) <= .5 for five,ten in ((h5,h10),(a5,a10)))
    gap = max(abs(value) for value in difference.values()) if difference else None
    medium = min(len(home_rows),len(away_rows)) >= 8 and min(len(home_split),len(away_split)) >= 5 and len(baseline) >= 50 and agreement and gap is not None and gap <= .15
    high = medium and min(len(home_rows),len(away_rows)) >= 10 and min(len(home_split),len(away_split)) >= 8 and len(baseline) >= 100 and consensus['bookmaker_count'] >= 2 and gap <= .1
    confidence = 'high' if high and competition_type == 'club' else 'medium' if medium else 'low'
    return {**base, **probabilities, 'status': 'ok', 'expected_home_goals': eh, 'expected_away_goals': ea,
            'confidence': confidence, 'model_market_difference': difference,
            'confidence_rules': {'recent_longer_form_agree': agreement, 'medium_sample_rules': medium,
                                 'high_sample_rules': high, 'market_max_difference': gap,
                                 'national_high_confidence_disabled': competition_type == 'national'}}, extra
