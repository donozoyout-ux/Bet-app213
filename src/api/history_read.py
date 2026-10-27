"""One history read service for existing HTTP handlers and server analytics.

The archive SQL session has the same model/schema and query semantics as hot
storage. It is explicitly scoped; partial SQL result sets are never merged.
"""
from contextlib import asynccontextmanager
import re
import os
from fastapi import HTTPException
from src.archive.history_reader import configured_reader, HistoryUnavailable
from src.archive.history_format import timestamp

READ_PATHS = frozenset(('/api/leagues', '/api/bookmakers', '/api/matches',
    '/api/performance-trends', '/api/market-performance', '/api/history/seasons',
    '/api/chart-diagnostics', '/api/learning/summary', '/api/learning/coverage',
    '/api/learning/reports', '/api/learning/statistics', '/api/learning/lessons'))


@asynccontextmanager
async def selected_reader(generation):
    if not generation:raise HTTPException(409,'Select an immutable history generation')
    if os.getenv('HISTORY_INDEX_ENABLED')=='true':
        from src.archive.history_index import configured_index
        async with configured_index().session(generation) as selected:yield selected
    else:
        reader=configured_reader()
        if generation!=reader.generation:raise HTTPException(409,'Select the configured immutable history generation')
        async with reader.session() as session:yield session,reader


@asynccontextmanager
async def archived_session(request, response):
    path = request.url.path
    allowed = path in READ_PATHS or re.fullmatch(r'/api/matches/\d+(?:/(?:dossier|history|prediction-audit|statistics|odds))?', path)
    allowed = allowed or re.fullmatch(r'/api/leagues/\d+/seasons', path)
    if request.method != 'GET' or not allowed:
        raise HTTPException(400, 'Archive is available only for historical reads')
    try:
        async with selected_reader(request.query_params.get('history_generation')) as (session,reader):
            coverage = reader.catalog['coverage']
            response.headers['X-History-Source'] = reader.catalog['source_id']
            response.headers['X-History-Generation'] = reader.generation
            response.headers['X-History-From'] = timestamp(coverage['from']).isoformat()
            response.headers['X-History-To'] = timestamp(coverage['to']).isoformat()
            response.headers['X-History-Leagues'] = ','.join(map(str, coverage['league_ids']))
            response.headers['Cache-Control'] = 'private, no-store'
            yield session
    except HistoryUnavailable:
        raise HTTPException(503, dict(code='history_archive_unavailable',
            message='Verified historical archive unavailable; no partial result returned.'),
            headers={'Retry-After': '60'}) from None
