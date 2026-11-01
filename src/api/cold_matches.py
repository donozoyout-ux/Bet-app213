"""Read-only fallback for existing match detail/odds contracts."""
import asyncio
import os
from datetime import datetime, timezone
from fastapi import HTTPException
from src.archive.cold_reader import HistoryUnavailable
from src.archive.retention_plan import classify_match
from .archive_history import history_reader, _read_slots


def cold_match_factory():
    # Configuration is evaluated only after a genuine hot miss.
    return history_reader


async def read_cold_match(match_id, response, factory, *, board_version=None,
                          revision=None, snapshot=None):
    record, page = await read_cold_record(match_id, factory, board_version=board_version,
                                          revision=revision, snapshot=snapshot)
    match = record['match']
    response.headers['X-History-Source'] = 'github_archive'
    response.headers['X-History-Snapshot'] = page['snapshot']
    response.headers['X-History-Board-Version'] = record['board_version']
    response.headers['X-History-Revision'] = str(record['revision'])
    response.headers['X-History-Source-Parity'] = 'unverified'
    response.headers['X-History-Cleanup-Authorized'] = 'false'
    return {**match, 'raw': {}, 'odds': [{**odd, 'raw': {}} for odd in match['odds']]}


async def read_cold_record(match_id, factory, *, board_version=None,
                           revision=None, snapshot=None):
    explicit = any(value is not None for value in (board_version, revision, snapshot))
    if os.getenv('HISTORY_ARCHIVE_ENABLED') != 'true' and not explicit:
        raise HTTPException(404, 'Match not found')
    try:
        async with asyncio.timeout(10):
            async with _read_slots:
                reader = factory()
                page = await reader.page(match_id=match_id, board_version=board_version,
                                         revision=revision, snapshot=snapshot, limit=1)
        if not page['items']:
            raise HTTPException(404, 'Match not found in pinned archive')
        # Without an explicit immutable version, guessing between boards is unsafe.
        if board_version is None and page['board_versions'] > 1:
            raise HistoryUnavailable('history_version_required')
        record = page['items'][0]
        match = record['match']
        kickoff = datetime.fromisoformat(match['kickoff_at'])
        if (classify_match(kickoff, match['status'], datetime.now(timezone.utc))
                != 'archive_candidate_pending_evidence'
                or match['ft_home'] is None or match['ft_away'] is None
                or not record['evidence']
                or any(e['outcome'] == 'pending' or e['revision'] != record['revision']
                       or e['home_score'] != match['ft_home'] or e['away_score'] != match['ft_away']
                       for e in record['evidence'])):
            raise HistoryUnavailable('history_hot_record_required')
        return record, page
    except (HistoryUnavailable, TimeoutError):
        raise HTTPException(503, detail={'code': 'history_archive_unavailable',
                            'message': 'Geçmiş arşivi doğrulanamıyor; yayın sürümü veya kaynak kanıtı eksik olabilir.'}) from None
