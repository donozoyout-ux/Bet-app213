"""Explicit archive history view. Disabled until owner pins a verified manifest."""
import os
import asyncio
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query
from src.archive.cold_reader import ColdReader, HistoryUnavailable

router = APIRouter()
_read_slots = asyncio.Semaphore(2)


def history_reader():
    if os.getenv('HISTORY_ARCHIVE_ENABLED') != 'true':
        raise HTTPException(503, detail={'code':'history_archive_disabled', 'message':'Geçmiş arşivi henüz doğrulanmadı.'})
    try:
        return ColdReader(os.environ['HISTORY_ARCHIVE_REPOSITORY'],
                          os.environ['HISTORY_ARCHIVE_TOKEN'], os.environ['HISTORY_ARCHIVE_KEY'],
                          os.environ['HISTORY_ARCHIVE_MANIFEST_ASSET'],
                          os.environ['HISTORY_ARCHIVE_MANIFEST_SHA256'])
    except Exception:
        raise HTTPException(503, detail={'code':'history_archive_unavailable'}) from None


@router.get('/history/archive')
async def archive_history(day: date | None = None, match_id: int | None = Query(None, gt=0),
                          board_version: str | None = Query(None, pattern='^[a-f0-9]{64}$'),
                          limit: int = Query(25, ge=1, le=50), offset: int = Query(0, ge=0, le=50000),
                          snapshot: str | None = Query(None, pattern='^[a-f0-9]{64}$'),
                          reader=Depends(history_reader)):
    try:
        async with asyncio.timeout(30):
            async with _read_slots:
                return await reader.page(day=day.isoformat() if day else None, match_id=match_id,
                                         board_version=board_version, limit=limit, offset=offset, snapshot=snapshot)
    except (HistoryUnavailable, TimeoutError):
        raise HTTPException(503, detail={'code':'history_archive_unavailable',
                            'message':'Geçmiş arşivi doğrulanamıyor. Lütfen daha sonra tekrar deneyin.'}) from None
