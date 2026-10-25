"""Bounded, read-only chart cohort counts; never generate retrospective picks."""
from collections import Counter
from datetime import timedelta
from sqlalchemy import select, func, case
from sqlalchemy.orm import defer
from src.models import Match, MatchStatistics, PredictionSnapshot, utcnow
from src.jobs.worker import aware
from .performance import display_result
from .settlements import decisions_many
from .performance_trends import ISTANBUL

MAX_SNAPSHOTS = 5000


async def chart_diagnostics(session, now=None):
    now = now or utcnow()
    today = now.astimezone(ISTANBUL).date()
    total, finished = (await session.execute(select(func.count(Match.id),
        func.count(case((Match.status == 'finished', 1)))))).one()
    records = (await session.execute(select(PredictionSnapshot, Match, MatchStatistics)
        .join(Match, Match.id == PredictionSnapshot.match_id)
        .outerjoin(MatchStatistics, MatchStatistics.match_id == Match.id)
        .options(defer(Match.raw), defer(MatchStatistics.raw))
        .order_by(PredictionSnapshot.captured_at.desc(), PredictionSnapshot.match_id.desc())
        .limit(MAX_SNAPSHOTS + 1))).all()
    truncated = len(records) > MAX_SNAPSHOTS
    records = records[:MAX_SNAPSHOTS]
    decisions = await decisions_many(session, [m.id for s, m, stats in records]) if records else {}
    counts = Counter(); cohorts = {days: Counter() for days in (7, 30)}
    captures = []; latest = None
    for snapshot, match, stats in records:
        if not match.kickoff_at or aware(snapshot.captured_at) >= aware(match.kickoff_at):
            counts['invalid_snapshots'] += 1
            continue
        counts['prematch_snapshots'] += 1
        captures.append(aware(snapshot.captured_at))
        day = aware(match.kickoff_at).astimezone(ISTANBUL).date()
        if day < today - timedelta(days=30): counts['old_snapshots'] += 1
        if match.status == 'finished': counts['finished_snapshots'] += 1
        seen = set()
        for index, pick in enumerate(snapshot.picks):
            identity = (pick.get('market'), pick.get('selection'))
            if identity in seen:
                counts['duplicates_excluded'] += 1
                continue
            seen.add(identity)
            counts['saved_picks'] += 1
            saved = decisions.get(match.id, {}).get(index)
            outcome, _, _ = display_result(snapshot, match, stats, pick, saved)
            if not saved and outcome in {'won', 'lost'}: outcome = 'awaiting_data'
            if saved:
                counts['persisted_decisions'] += 1
                latest = max(latest, aware(saved.recorded_at)) if latest else aware(saved.recorded_at)
            counts[outcome] += 1
            if day >= today: counts['today_and_future_picks'] += 1
            for days, cohort in cohorts.items():
                if today - timedelta(days=days) <= day < today:
                    cohort['saved_picks'] += 1
                    cohort[outcome] += 1
    def outcomes(c):
        return dict(saved_picks=c['saved_picks'], settled=c['won']+c['lost'],
            **{key:c[key] for key in ('won','lost','pending','awaiting_data','disputed','void')})
    from src.jobs.settlement import health
    worker_health = await health(session, now)
    return dict(checked_at=now, cache_seconds=60, truncated=truncated, sample_limit=MAX_SNAPSHOTS,
        scope='all_saved_prematch_picks_unfiltered', matches=dict(total=total, finished=finished),
        snapshots=dict(examined=len(records), prematch=counts['prematch_snapshots'],
            invalid_excluded=counts['invalid_snapshots'], finished=counts['finished_snapshots'],
            older_than_30_days=counts['old_snapshots'], duplicates_excluded=counts['duplicates_excluded'],
            first_capture=min(captures) if captures else None, last_capture=max(captures) if captures else None),
        outcomes=outcomes(counts), today_and_future_picks=counts['today_and_future_picks'],
        cohorts={str(days):outcomes(c) for days,c in cohorts.items()},
        settlement_worker=dict(**worker_health,
            persisted_decisions=counts['persisted_decisions'], last_decision_at=latest),
        explanation='bounded_sample' if truncated else 'no_finished_captured_matches' if not counts['finished_snapshots']
            else 'no_verified_settlements' if not counts['won']+counts['lost'] else 'verified_settlements_available')
