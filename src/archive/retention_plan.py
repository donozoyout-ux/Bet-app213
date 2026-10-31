"""Offline planning only: no connections, mutation or cleanup authorization."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def active_window(now):
    if now.tzinfo is None:
        raise ValueError('timezone-aware clock required')
    day = now.astimezone(ZoneInfo('Europe/Istanbul')).date()
    start = datetime.combine(day, time(), ZoneInfo('Europe/Istanbul'))
    return start.astimezone(timezone.utc), (start + timedelta(days=4)).astimezone(timezone.utc)


def classify_match(kickoff, status, now):
    start, end = active_window(now)
    if kickoff is None:
        return 'retain_unknown_date'
    if kickoff.tzinfo is None:
        raise ValueError('timezone-aware kickoff required')
    if start <= kickoff < end:
        return 'retain_active'
    if kickoff >= end:
        return 'retain_future'
    if status != 'finished':
        return 'retain_unresolved'
    return 'archive_candidate_pending_evidence'


def disk_scenario(database_bytes, candidate_payload_bytes):
    if not 0 <= candidate_payload_bytes <= database_bytes:
        raise ValueError('invalid measured sizes')
    return {
        'before_bytes': database_bytes,
        'after_this_plan_bytes': database_bytes,
        'guaranteed_reclaimed_bytes': 0,
        'illustrative_after_rewrite_bytes': database_bytes - candidate_payload_bytes,
        'cleanup_authorized': False,
    }
