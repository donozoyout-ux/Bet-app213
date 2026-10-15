"""Durable decisions over existing snapshots. No predictions are generated here."""
from copy import deepcopy
import hashlib
import json
from sqlalchemy import select
from src.models import PredictionSnapshot, PredictionSettlement, Match, MatchStatistics, utcnow

VERSION = 'settlement-v1'
TERMINAL = {'won', 'lost', 'void'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def snapshot_digest(snapshot):
    from src.jobs.worker import aware
    return digest(dict(match_id=snapshot.match_id, league_id=snapshot.league_id,
        captured_at=aware(snapshot.captured_at).isoformat(), model_version=snapshot.model_version, picks=snapshot.picks))


def evidence(match, stats):
    fields = ('home_corners', 'away_corners', 'home_yellow_cards', 'away_yellow_cards', 'home_red_cards', 'away_red_cards')
    return dict(status=match.status, ft_home=match.ft_home, ft_away=match.ft_away,
        ht_home=match.ht_home, ht_away=match.ht_away,
        statistics={k: getattr(stats, k, None) for k in fields},
        is_final=bool(stats and stats.is_final), source=getattr(stats, 'source', None),
        statistics_updated_at=stats.updated_at.isoformat() if stats and stats.updated_at else None,
        score_observed_at=match.status_observed_at.isoformat() if match.status_observed_at else None)


async def decisions_many(session, match_ids):
    rows = await session.scalars(select(PredictionSettlement).where(PredictionSettlement.match_id.in_(match_ids))
        .order_by(PredictionSettlement.revision))
    result = {}
    for row in rows:
        result.setdefault(row.match_id, {})[row.pick_index] = row
    return result


async def decisions(session, match_id):
    return (await decisions_many(session, [match_id])).get(match_id, {})


async def settle_snapshot(session, snapshot, match, stats):
    """First terminal result wins, even under concurrent workers. Caller commits."""
    from src.analytics.performance import settle
    from src.jobs.worker import aware
    if not match.kickoff_at or aware(snapshot.captured_at) >= aware(match.kickoff_at):
        return 0
    if session.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    existing = await decisions(session, match.id)
    count = 0
    for index, pick in enumerate(snapshot.picks):
        if index in existing:
            continue
        outcome, actual = settle(match, stats, pick)
        if outcome not in TERMINAL:
            continue
        proof = evidence(match, stats)
        result = await session.execute(insert(PredictionSettlement).values(match_id=match.id,
            pick_index=index, revision=1, outcome=outcome, actual={'value': actual}, evidence=proof,
            snapshot_hash=snapshot_digest(snapshot), evidence_hash=digest(proof), evaluator_version=VERSION,
            recorded_at=utcnow(), actor='worker', reason='first_verified_terminal_observation')
            .on_conflict_do_nothing(index_elements=['match_id', 'pick_index', 'revision']))
        count += result.rowcount
    return count


async def settle_match(session, match):
    if match.status not in {'finished','cancelled','abandoned'}:
        return 0
    snapshot = await session.get(PredictionSnapshot, match.id)
    if snapshot is None:
        return 0
    await session.flush()
    return await settle_snapshot(session, snapshot, match, await session.get(MatchStatistics, match.id))


async def correct_settlement(session, match_id, pick_index, *, expected_revision, actor, reason):
    """Explicit trusted administrative call, never exposed through a public route.

    Append a recomputed decision with compare-and-swap revision; conflicting calls
    fail the unique constraint and must be reviewed again. Never overwrite history.
    """
    from src.analytics.performance import settle
    if not actor.strip() or not reason.strip() or len(actor)>120 or len(reason)>500:
        raise ValueError('A named actor and correction reason are required')
    snapshot = await session.get(PredictionSnapshot, match_id)
    previous = (await decisions(session, match_id)).get(pick_index)
    if previous is None or previous.revision != expected_revision:
        raise ValueError('Settlement revision changed or missing')
    if snapshot_digest(snapshot) != previous.snapshot_hash:
        raise ValueError('Snapshot integrity mismatch')
    match = await session.get(Match, match_id)
    stats = await session.get(MatchStatistics, match_id)
    outcome, actual = settle(match, stats, snapshot.picks[pick_index])
    if outcome not in TERMINAL:
        raise ValueError('Correction requires complete terminal evidence')
    if (outcome, actual) == (previous.outcome, previous.actual['value']):
        return previous
    proof = evidence(match, stats)
    row = PredictionSettlement(match_id=match_id, pick_index=pick_index, revision=expected_revision+1,
        outcome=outcome, actual={'value': actual}, evidence=proof, snapshot_hash=previous.snapshot_hash,
        evidence_hash=digest(proof), evaluator_version=VERSION, actor=actor, reason=reason)
    session.add(row)
    await session.flush()
    return row


async def resolved_predictions(session, *, after_id=0, limit=200):
    """Read-only, bounded cursor over verified first terminal records for Lessons.

    Includes voids explicitly; consumers must exclude them for win/loss training.
    Frozen revision 1 keeps a stable cursor; corrections are returned as latest.
    No enabled-league filter: disabling a league cannot erase its learning history.
    """
    if not 1 <= limit <= 1000 or after_id < 0:
        raise ValueError('Invalid cursor or limit')
    query = select(PredictionSettlement, PredictionSnapshot, Match).join(
        PredictionSnapshot, PredictionSnapshot.match_id == PredictionSettlement.match_id).join(
        Match, Match.id == PredictionSnapshot.match_id).where(
        PredictionSettlement.revision == 1, PredictionSettlement.id > after_id,
        PredictionSnapshot.captured_at < Match.kickoff_at).order_by(PredictionSettlement.id).limit(limit)
    items = []
    from src.jobs.worker import aware
    page = (await session.execute(query)).all()
    latest_rows = await decisions_many(session, {row[2].id for row in page}) if page else {}
    for original, snapshot, match in page:
        latest = latest_rows[match.id][original.pick_index]
        if not match.kickoff_at or aware(snapshot.captured_at) >= aware(match.kickoff_at):
            continue
        if snapshot_digest(snapshot) != latest.snapshot_hash or digest(latest.evidence) != latest.evidence_hash:
            raise ValueError('Prediction history integrity mismatch')
        from types import SimpleNamespace
        from src.analytics.performance import settle
        proof=latest.evidence
        proof_match=SimpleNamespace(**proof)
        proof_stats=SimpleNamespace(is_final=proof['is_final'], **proof['statistics'])
        if latest.evaluator_version != VERSION or settle(proof_match,proof_stats,snapshot.picks[original.pick_index]) != (latest.outcome,latest.actual['value']):
            raise ValueError('Recorded decision does not match its evidence')
        items.append(dict(cursor=original.id, match_id=match.id, pick_index=original.pick_index,
            captured_at=aware(snapshot.captured_at), model_version=snapshot.model_version,
            prediction=deepcopy(snapshot.picks[original.pick_index]), outcome=latest.outcome,
            actual=deepcopy(latest.actual['value']), revision=latest.revision,
            evidence=deepcopy(latest.evidence), recorded_at=latest.recorded_at))
    return items


async def settle_batch(session, *, after_match_id=0, limit=100):
    """Bounded legacy backfill. Returns last examined match for durable caller cursor."""
    if not 1 <= limit <= 1000 or after_match_id < 0:
        raise ValueError('Invalid batch cursor or limit')
    rows = (await session.execute(select(PredictionSnapshot, Match, MatchStatistics).join(
        Match, Match.id == PredictionSnapshot.match_id).outerjoin(MatchStatistics, MatchStatistics.match_id == Match.id)
        .where(Match.id > after_match_id, Match.status.in_(['finished','cancelled','abandoned']))
        .order_by(Match.id).limit(limit))).all()
    for snapshot, match, stats in rows:
        await settle_snapshot(session, snapshot, match, stats)
    return rows[-1][1].id if rows else 0
