"""Public coverage and identity resolver; secrets and asset IDs remain server-side."""
from collections import Counter
import os
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from src.models import Match, League
from src.db import database
from src.archive.history_index import configured_index
from src.archive.history_reader import HistoryUnavailable
from src.jobs.storage_admission import status

router=APIRouter(prefix='/history')


@router.get('/coverage')
async def coverage():
    capacity=status(database.engine)
    if os.getenv('HISTORY_INDEX_ENABLED')!='true':
        return dict(status='disabled',verified_production_archive=False,capacity=capacity,partitions=[])
    try:
        index=configured_index();data=await index.prepare()
        report=data['coverage']
        reasons=Counter(r['reason'].split(':')[0] for r in report['retained'])
        return dict(status='verified',run_id=index.run_id,created_at=data['created_at'],source_id=data['source_id'],
            verified_production_archive=True,covered=len(report['covered_ids']),retained=len(report['retained']),
            retained_reasons=dict(reasons),capacity=capacity,
            partitions=[dict(generation=p['generation'],matches=len(p['matches']),
                leagues=sorted({m['league'] for m in p['matches']}),seasons=sorted({m['season'] for m in p['matches']}),
                href='/api/matches?history_source=archive&history_generation='+p['generation']) for p in data['partitions']])
    except HistoryUnavailable:
        raise HTTPException(503,dict(code='history_index_unavailable',verified_production_archive=False)) from None


@router.get('/resolve/{source_match_id}')
async def resolve(source_match_id:int,provider:str=Query('goaloo',pattern='^[a-zA-Z0-9_-]{1,30}$')):
    # Provider ID, not a potentially colliding database integer ID.
    if database.configured and database.ready:
        async with database.session() as session:
            mid=await session.scalar(select(Match.id).join(League).where(Match.external_match_id==source_match_id,League.source==provider))
            if mid is not None:return dict(source='hot',href=f'/match/{mid}',match_id=mid)
    try:
        index=configured_index();data=await index.prepare()
        for part in data['partitions']:
            for match in part['matches']:
                if match['provider']==provider and match['external_match_id']==source_match_id:
                    return dict(source='archive',identity=match['identity'],match_id=match['id'],
                        href=f'/match/{match["id"]}?history_source=archive&history_generation={part["generation"]}')
    except HistoryUnavailable:
        raise HTTPException(503,'Verified history unavailable') from None
    raise HTTPException(404,'Source match identity not found in verified coverage')
