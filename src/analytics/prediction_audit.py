"""Read-only audit and forward-only publication journal; never infer old releases."""
from copy import deepcopy
import math
from sqlalchemy import select, func
from src.models import PredictionPublication, PredictionSnapshot, Match
from src.jobs.worker import aware


FIELDS = ('id', 'market', 'selection', 'label', 'probability', 'reliability',
          'sample_size', 'confidence', 'line', 'period', 'engine_version')


def selections(items):
    return [{k: deepcopy(p[k]) for k in FIELDS if k in p} for p in items]


def identity(pick):
    return tuple(str(pick.get(k, '')) for k in ('market', 'selection', 'id', 'line', 'period'))


def problems(picks):
    issues = []
    if not isinstance(picks, list):
        return ['invalid_picks']
    seen = set()
    for p in picks:
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ('id', 'market', 'selection')):
            issues.append('invalid_selection'); continue
        key = identity(p)
        if key in seen: issues.append('duplicate_selection')
        seen.add(key)
        value = p.get('probability')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            issues.append('invalid_probability')
    return sorted(set(issues))


def compare(saved, current):
    if problems(saved) or problems(current):
        return dict(status='invalid', added=[], removed=[], changed=[])
    before, after = ({identity(p): p for p in rows} for rows in (saved, current))
    added = [p for k, p in after.items() if k not in before]
    removed = [p for k, p in before.items() if k not in after]
    changed = [dict(before=before[k], after=p) for k, p in after.items()
               if k in before and selections([before[k]]) != selections([p])]
    return dict(status='different' if added or removed or changed else 'same',
                added=added, removed=removed, changed=changed)


async def record_publication(session, board, version, model, data, now):
    """Called only by the existing serialized writer, in its board transaction.

    Unchanged renewals do not grow history. A -> B -> A and withdrawals do.
    First observation after upgrade is stamped now, never backdated.
    """
    current = {i['match']['id']: i for i in data.get('items', [])}
    previous = {i['match']['id'] for i in (board.payload or {}).get('items', [])}
    ids = set(current) | previous
    if not ids: return
    latest = select(func.max(PredictionPublication.id)).where(
        PredictionPublication.match_id.in_(ids)).group_by(PredictionPublication.match_id)
    rows = {r.match_id: r for r in await session.scalars(select(PredictionPublication).where(
        PredictionPublication.id.in_(latest)))}
    matches = {m.id: m for m in await session.scalars(select(Match).where(Match.id.in_(ids)))}
    for mid in sorted(ids):
        match = matches.get(mid)
        if not match or not match.kickoff_at: continue
        picks = selections(current[mid].get('recommendations', [])[:2]) if mid in current else []
        old = rows.get(mid)
        if old and old.picks == picks and old.model_version == model and aware(old.kickoff_at) == aware(match.kickoff_at):
            continue
        if old is None and not picks: continue
        session.add(PredictionPublication(match_id=mid, league_id=board.league_id,
            board_version=version, model_version=model, published_at=now,
            kickoff_at=match.kickoff_at, picks=picks))


async def audit(session, match, now, limit=25, before=None):
    from src.api.match_page import current_recommendations
    live = await current_recommendations(session, match, now)
    snapshot = await session.get(PredictionSnapshot, match.id)
    issues = []
    original = None
    if snapshot is None:
        issues.append('missing_snapshot')
    else:
        issues.extend(problems(snapshot.picks))
        if not snapshot.picks: issues.append('empty_snapshot')
        if not snapshot.model_version: issues.append('missing_model_version')
        if snapshot.league_id != match.league_id: issues.append('league_mismatch')
        if not match.kickoff_at: issues.append('missing_kickoff')
        elif aware(snapshot.captured_at) >= aware(match.kickoff_at): issues.append('not_prematch')
        if aware(snapshot.captured_at) > now: issues.append('future_snapshot')
        original = dict(picks=snapshot.picks, published_at=snapshot.captured_at, model_version=snapshot.model_version)
    query = select(PredictionPublication).where(PredictionPublication.match_id == match.id)
    if before is not None: query = query.where(PredictionPublication.id < before)
    rows = list(await session.scalars(query.order_by(PredictionPublication.id.desc()).limit(limit + 1)))
    history = []
    for i, row in enumerate(rows[:limit]):
        prior = rows[i+1] if i+1 < len(rows) else None
        history.append(dict(id=row.id, published_at=row.published_at, model_version=row.model_version,
            board_version=row.board_version, kickoff_at=row.kickoff_at, picks=row.picks,
            issues=problems(row.picks), changes=compare(prior.picks, row.picks) if prior else None))
    if not rows: issues.append('publication_history_unavailable')
    current = live['current_recommendations']
    if not current: issues.append('current_publication_unavailable')
    issues.extend('current_' + p for p in problems(current))
    valid = snapshot is not None and not any(p not in ('publication_history_unavailable', 'current_publication_unavailable') for p in issues)
    difference = compare(snapshot.picks, current) if valid and current else dict(status='unavailable', added=[], removed=[], changed=[])
    return dict(match_id=match.id, original=original, **live, comparison=difference, issues=sorted(set(issues)),
        history=history, next_before=rows[limit-1].id if len(rows)>limit else None,
        history_note='Geçmiş yalnızca bu özellik devreye girdikten sonra gözlenen yayınları kapsar; eski yayınlar yeniden üretilmez.')
