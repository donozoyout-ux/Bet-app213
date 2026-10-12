"""Market adapters over one leakage-free legacy model snapshot.

Calibration is reported honestly: the existing Poisson model is uncalibrated.
No price, bookmaker or coupon decision belongs to an engine.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import os
import time
from typing import Mapping, Protocol

from .predictions import Result, predict

log = logging.getLogger(__name__)
VERSION = 'specialist-adapters-v1'
PLANNED = ('first_half_result', 'first_half_goals', 'team_goals',
           'asian_handicap', 'corners', 'goal_bands', 'parity', 'correct_score',
           'HT/FT', 'combo')


@dataclass(frozen=True)
class PrematchContext:
    match_id: int
    kickoff_at: datetime | None
    observed_at: datetime
    cutoff: datetime
    history: tuple[Result, ...]
    prediction: Mapping

    @classmethod
    def load(cls, match_id, kickoff_at, observed_at, history, home_id, away_id,
             competition_type, consensus=None, predictor=predict):
        if observed_at.tzinfo is None or (kickoff_at and kickoff_at.tzinfo is None):
            raise ValueError('Timezone-aware observation and kickoff required')
        cutoff = min(observed_at, kickoff_at) if kickoff_at else observed_at
        # SQL caller also requires created/updated/source observation <= cutoff.
        rows = tuple(r for r in history if r.id != match_id and
                     r.kickoff_at <= cutoff - timedelta(hours=3))
        prediction, extra = predictor(rows, home_id, away_id, competition_type, consensus)
        if kickoff_at is None:
            prediction = {k: v for k, v in prediction.items()
                          if 'probability' not in k and not k.startswith('expected_')}
            prediction.update(status='insufficient_data')
        return cls(match_id, kickoff_at, observed_at, cutoff, rows, prediction), extra


class MarketEngine(Protocol):
    market_id: str
    required_inputs: tuple[str, ...]

    def is_supported(self, context: PrematchContext) -> bool: ...
    def predict(self, context: PrematchContext) -> dict: ...


class LegacyAdapter:
    required_inputs = ('prematch_history', 'venue_rates', 'score_distribution')
    keys: dict[str, str] = {}

    def is_supported(self, context):
        return context.kickoff_at is not None and context.prediction['status'] == 'ok'

    def predict(self, context):
        p = context.prediction
        return {selection: p[key] for selection, key in self.keys.items()}


class ResultEngine(LegacyAdapter):
    market_id = 'result_1x2'
    keys = {s: s + '_probability' for s in ('home', 'draw', 'away')}


class TotalGoalsEngine(LegacyAdapter):
    market_id = 'total_goals'
    keys = {f'{side}_{line}': f'{side}_{line.replace("_", "")}_probability'
            for line in ('1_5', '2_5', '3_5') for side in ('over', 'under')}


class DoubleChanceEngine(LegacyAdapter):
    market_id = 'double_chance'

    def predict(self, context):
        p = context.prediction
        return {s: sum(p[k + '_probability'] for k in outcomes)
                for s, outcomes in [('1x', ('home', 'draw')),
                                    ('x2', ('draw', 'away')), ('12', ('home', 'away'))]}


class BothTeamsToScoreEngine(LegacyAdapter):
    market_id = 'both_teams_to_score'
    keys = {'yes': 'btts_probability', 'no': 'no_btts_probability'}


class EngineRegistry:
    def __init__(self, engines=None, disabled=()):
        self.engines = {e.market_id: e for e in (engines if engines is not None else
                       (ResultEngine(), TotalGoalsEngine(), DoubleChanceEngine(), BothTeamsToScoreEngine()))}
        self.disabled = set(disabled)

    def run(self, context):
        reports = {}
        for market_id, engine in self.engines.items():
            start = time.perf_counter()
            report = dict(market_id=market_id, version=VERSION,
                          required_inputs=list(engine.required_inputs),
                          calibration={'status': 'unvalidated', 'version': None},
                          sample_size=context.prediction.get('sample_size', 0),
                          evidence={k: context.prediction.get(k) for k in
                                    ('league_sample_size', 'home_sample_size', 'away_sample_size',
                                     'home_venue_sample_size', 'away_venue_sample_size')},
                          probabilities={}, status='unavailable', missing_reason=None)
            try:
                if market_id in self.disabled:
                    report['missing_reason'] = 'engine_disabled'
                elif not engine.is_supported(context):
                    report['missing_reason'] = 'insufficient_prematch_data'
                else:
                    values = engine.predict(context)
                    if not values or any(not isinstance(v, (int, float)) or not math.isfinite(v)
                                         or not 0 <= v <= 1 for v in values.values()):
                        raise ValueError('Invalid market probabilities')
                    report.update(status='available', probabilities=values)
            except Exception as exc:
                report['missing_reason'] = 'engine_error:' + type(exc).__name__
                log.exception('Market engine failed market=%s match=%s', market_id, context.match_id)
            report['duration_ms'] = (time.perf_counter() - start) * 1000
            reports[market_id] = report
        return reports


def configured_registry():
    return EngineRegistry(disabled=filter(None, os.getenv('DISABLED_MARKET_ENGINES', '').split(',')))


LEGACY_MARKETS = {'result_1x2': 'result', 'total_goals': 'goals',
                  'double_chance': 'double_chance', 'both_teams_to_score': 'btts'}


def attach_coverage(reports, candidates):
    for market_id, report in reports.items():
        rows = [c for c in candidates if c['market'] == LEGACY_MARKETS[market_id]]
        report['coverage'] = dict(selections=len(rows), strong=sum(c.get('qualifies', False) for c in rows),
                                 priced=sum(c['verified_odds']['has_odds'] for c in rows),
                                 fresh=sum(c['verified_odds'].get('coupon_eligible', False) for c in rows),
                                 missing_reasons=sorted({c['verified_odds']['reason'] for c in rows
                                                         if c['verified_odds'].get('reason')}))


def evaluate_binary(observations):
    """Brier and calibration bins for genuine out-of-sample (p, outcome) pairs."""
    rows = list(observations)
    if any(not math.isfinite(p) or not 0 <= p <= 1 or y not in (0, 1) for p, y in rows):
        raise ValueError('Invalid probability/outcome')
    bins = []
    for index in range(10):
        bucket = [(p, y) for p, y in rows if min(9, int(p * 10)) == index]
        bins.append(dict(count=len(bucket), predicted=sum(p for p, _ in bucket)/len(bucket) if bucket else None,
                         observed=sum(y for _, y in bucket)/len(bucket) if bucket else None))
    return dict(sample_size=len(rows), brier=sum((p-y)**2 for p, y in rows)/len(rows) if rows else None,
                calibration=bins, status='measured' if rows else 'unavailable')


def historical_backtest(fixtures, registry=None):
    """Evaluate saved as-of contexts against later confirmed full-time outcomes.

    Callers supply observation-filtered history, never today's corrected history.
    Empty markets retain explicit unavailable metrics; no calibration is invented.
    """
    registry = registry or EngineRegistry()
    observations = {market: {} for market in registry.engines}
    for context, outcome in fixtures:
        if outcome.id != context.match_id or outcome.kickoff_at != context.kickoff_at:
            raise ValueError('Outcome identity does not match snapshot')
        if context.cutoff > outcome.kickoff_at or any(
                r.id == outcome.id or r.kickoff_at > context.cutoff-timedelta(hours=3)
                for r in context.history):
            raise ValueError('Snapshot contains future information')
        h, a = outcome.home_goals, outcome.away_goals
        if h < 0 or a < 0:
            raise ValueError('Unconfirmed outcome')
        home, draw, away = h > a, h == a, h < a
        labels = {'result_1x2': dict(home=home, draw=draw, away=away),
                  'double_chance': {'1x': home or draw, 'x2': draw or away, '12': home or away},
                  'both_teams_to_score': {'yes': h > 0 and a > 0, 'no': not (h > 0 and a > 0)},
                  'total_goals': {f'{side}_{str(line).replace(".", "_")}':
                                  h+a > line if side=='over' else h+a < line
                                  for line in (1.5, 2.5, 3.5) for side in ('over', 'under')}}
        for market, report in registry.run(context).items():
            if report['status'] != 'available':
                continue
            for selection, probability in report['probabilities'].items():
                observations[market].setdefault(selection, []).append((probability, int(labels[market][selection])))
    return {market: {selection: evaluate_binary(rows) for selection, rows in selections.items()}
            for market, selections in observations.items()}
