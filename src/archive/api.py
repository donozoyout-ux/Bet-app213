"""Opt-in, minimal historical summary. Never substitute archived data for live APIs."""
from datetime import datetime, timezone
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from src.archive.crypto import read

router=APIRouter()


def summary(data):
    latest={}
    for d in data['decisions']:
        key=(d['match_id'],d['pick_index'])
        if key not in latest or latest[key]['revision']<d['revision']:latest[key]=d
    return dict(mode='historical_archive',live=False,as_of=data['created_at'],
                forecast_records=len(data['forecasts']),recorded_decisions=len(latest),
                recorded_won=sum(d['outcome']=='won' for d in latest.values()),
                recorded_lost=sum(d['outcome']=='lost' for d in latest.values()),
                recorded_void=sum(d['outcome']=='void' for d in latest.values()),
                warning='Arşiv · Canlı veri değildir. Kayıtlı kararlar güncel maç verisiyle yeniden doğrulanmamıştır. Başarı veya gelişim kanıtı sunulmaz.')


@router.get('/api/archive-summary')
async def archive_summary():
    if os.getenv('ARCHIVE_PUBLIC_SUMMARY_APPROVED')!='true':raise HTTPException(503,'Historical summary is not approved or configured')
    path=os.getenv('ARCHIVE_FILE');key=os.getenv('ARCHIVE_ENCRYPTION_KEY')
    if not path or not key:raise HTTPException(503,'Historical archive unavailable')
    try:
        import asyncio
        data=await asyncio.to_thread(read,path,key)
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(data['created_at'])).total_seconds()
        if age< -300:raise ValueError('Future archive')
        from fastapi.responses import JSONResponse
        return JSONResponse({**summary(data),'age_seconds':max(0,int(age)),'stale':age>86400},headers={'Cache-Control':'no-store'})
    except Exception:raise HTTPException(503,'Historical archive authentication or validation failed') from None


@router.get('/archive',response_class=HTMLResponse,include_in_schema=False)
async def archive_page():
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).with_name('dashboard.html'),media_type='text/html',headers={'Cache-Control':'no-store'})
