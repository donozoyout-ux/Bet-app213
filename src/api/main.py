"""BetApp213 HTTP application. Scraping is performed by the durable job worker."""
from contextlib import asynccontextmanager
import asyncio
import logging
import os
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from fastapi import HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from src.config import settings
from src.db import database
from src.models import BoardEdition
from src.archive.r2 import archive_rows, canonical_json, health as r2_health, sha256
from src.archive.github_oidc import allowed_pilot_claims, verify_github_actions_token

log = logging.getLogger(__name__)
BACKGROUND_RETRY_SECONDS = 10


async def supervise_database():
    """Recover background failures without tying HTTP availability to PostgreSQL."""
    from src.jobs.worker import work, enqueue_initial_backfill
    while True:
        try:
            if not database.ready:
                await database.initialize()
            await enqueue_initial_backfill(database)
            if settings.worker_enabled:
                if settings.app_env == 'production':
                    await supervise_scraper_process()
                else:
                    await work()
            else:
                # Also detect/recover a database outage when scraping is disabled.
                async with database.engine.connect() as conn:
                    from sqlalchemy import text
                    await conn.execute(text('SELECT 1'))
        except asyncio.CancelledError:
            raise
        except Exception:
            database.ready = False
            log.exception('[BACKGROUND] task failed; retrying while HTTP remains available')
        await asyncio.sleep(BACKGROUND_RETRY_SECONDS)


async def supervise_scraper_process():
    """Isolate source parsing/CPU and connections on the existing free instance."""
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'src.jobs.runner',
        env={**os.environ, 'BETAPP_PROCESS_ROLE': 'scraper', 'GOALOO_CONCURRENCY': '1'})
    try:
        await child.wait()
        log.error('[BACKGROUND] scraper exited code=%s; durable jobs will resume', child.returncode)
    finally:
        if child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 5)
            except TimeoutError:
                child.kill()
                await child.wait()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # Do not make liveness/dashboard startup wait on a database connection timeout.
    worker = asyncio.create_task(supervise_database(), name='database-worker') if database.configured else None
    log.info('[BOOT] FastAPI started database_configured=%s worker_enabled=%s', database.configured, settings.worker_enabled)
    yield
    if worker:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    await database.close()

app = FastAPI(title='Kuponanalizii', version='2.0.0', lifespan=lifespan)
from src.api.capacity import DatabaseCapacityMiddleware
app.add_middleware(DatabaseCapacityMiddleware)
# Gunicorn's module-only target resolves `application`; Uvicorn continues using :app.
application = app


@app.exception_handler(TimeoutError)
async def request_timeout(request, exc):
    log.warning('[API] bounded timeout path=%s', request.url.path)
    return JSONResponse(status_code=504, content={'detail': 'API request timed out. Please retry.', 'code': 'api_timeout'})

@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logging.getLogger(__name__).error('Database request failed path=%s type=%s pool=%s',
        request.url.path, type(exc).__name__, database.engine.pool.status() if database.engine else 'unconfigured')
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable. Check server configuration.'})

@app.get('/health')
async def health():
    return {'status': 'ok', 'service': 'BetApp213'}


class BoardEditionArchiveRequest(BaseModel):
    limit: int = Field(ge=1, le=10)


@app.get('/api/storage/health')
async def storage_health():
    return await r2_health()


@app.post('/api/admin/storage/board-editions/archive')
async def archive_board_editions(body: BoardEditionArchiveRequest, authorization: str | None = Header(default=None)):
    import hmac
    if not settings.scraper_token:
        raise HTTPException(503, 'Storage administration is not configured.')
    if not authorization or not hmac.compare_digest(authorization, f'Bearer {settings.scraper_token}'):
        raise HTTPException(401, 'A valid Bearer token is required.')
    return await _run_board_edition_archive(body)


async def _run_board_edition_archive(body: BoardEditionArchiveRequest):
    if not settings.r2_enabled:
        raise HTTPException(503, 'R2 archival is disabled.')
    health_result = await r2_health()
    if not health_result.get('accessible'):
        raise HTTPException(503, {'detail': 'R2 is not accessible; archival was not started.',
            'error_class': health_result.get('error_class'), 'error_code': health_result.get('error_code'),
            'error_message_sanitized': health_result.get('error_message_sanitized')})
    if not database.configured or not database.ready:
        raise HTTPException(503, 'PostgreSQL is not ready.')
    async with database.session() as session:
        rows = (await session.execute(select(BoardEdition.version, BoardEdition.league_id,
            BoardEdition.created_at, BoardEdition.payload).order_by(BoardEdition.version).limit(body.limit))).all()
    results = await archive_rows(rows)

    # Verify the source rows still exist with the same identity and payload after R2 work.
    source_verified = True
    source_error_class = None
    if rows:
        try:
            versions = [row[0] for row in rows]
            async with database.session() as session:
                current_rows = (await session.execute(select(BoardEdition.version, BoardEdition.league_id,
                    BoardEdition.created_at, BoardEdition.payload).where(BoardEdition.version.in_(versions)))).all()
            current_by_version = {row[0]: row for row in current_rows}
            for source in rows:
                current = current_by_version.get(source[0])
                if (current is None or current[1] != source[1] or current[2] != source[2]
                        or sha256(canonical_json(current[3])) != sha256(canonical_json(source[3]))):
                    source_verified = False
                    break
        except Exception as exc:
            source_verified = False
            source_error_class = type(exc).__name__

    result_by_version = {result['version']: result for result in results}
    for row in rows:
        result = result_by_version.get(row[0])
        if result is not None:
            result['postgres_source_preserved'] = source_verified
    statuses = [result.get('status') for result in results]
    return {
        'requested_limit': body.limit,
        'records_selected': len(rows),
        'records_processed': len(results),
        'records_not_processed': max(0, len(rows) - len(results)),
        'records_archived': statuses.count('ARCHIVED'),
        'already_archived': statuses.count('ALREADY_ARCHIVED'),
        'hash_conflicts': statuses.count('HASH_CONFLICT'),
        'upload_failures': statuses.count('UPLOAD_FAILURE'),
        'readback_pass': sum(result.get('read_back_verified') is True for result in results),
        'readback_fail': sum(result.get('read_back_verified') is False for result in results),
        'manifest_pass': sum(result.get('manifest_read_back_verified') is True for result in results),
        'manifest_fail': sum(result.get('manifest_read_back_verified') is False for result in results),
        'postgres_source_preserved': source_verified,
        'source_verification_error_class': source_error_class,
        'postgres_writes': 0,
        'postgres_deletes': 0,
        'r2_objects_created': sum(result.get('object_created') is True for result in results),
        'stopped_on_failure': bool(results and statuses[-1] not in {'ARCHIVED', 'ALREADY_ARCHIVED'}),
        'results': results,
    }


@app.post('/api/admin/storage/board-editions/archive-pilot')
async def trigger_board_edition_archive_pilot(authorization: str | None = Header(default=None)):
    """Run exactly the first ten only from the pinned, manually dispatched GH workflow."""
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'A GitHub Actions OIDC Bearer token is required.')
    token = authorization[7:]
    try:
        claims = await asyncio.to_thread(verify_github_actions_token, token)
    except Exception as exc:
        # Do not log or reflect the bearer token or raw JWT exception.
        log.warning('[R2_PILOT] OIDC verification rejected error_class=%s', type(exc).__name__)
        raise HTTPException(401, 'GitHub Actions OIDC token could not be verified.') from None
    if not allowed_pilot_claims(claims):
        raise HTTPException(403, 'OIDC identity is not authorized for the R2 pilot.')
    # The request body cannot increase the pilot size: the cap is fixed in code.
    return await _run_board_edition_archive(BoardEditionArchiveRequest(limit=10))

@app.get('/', include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).resolve().with_name('dashboard.html'))


@app.get('/match/{match_id}', include_in_schema=False)
async def match_page(match_id: int):
    return FileResponse(Path(__file__).resolve().with_name('match.html'))


@app.get('/match-page.js', include_in_schema=False)
async def match_page_script():
    return FileResponse(Path(__file__).resolve().with_name('match-page.js'), media_type='application/javascript')


@app.get('/match-page.css', include_in_schema=False)
async def match_page_style():
    return FileResponse(Path(__file__).resolve().with_name('match-page.css'), media_type='text/css')


@app.get('/assets/kuponanalizii-logo.png', include_in_schema=False)
async def brand_logo():
    return FileResponse(Path(__file__).resolve().parent / 'assets' / 'kuponanalizii-logo.png',
                        media_type='image/png', headers={'Cache-Control': 'public, max-age=86400'})

@app.get('/assets/kuponanalizii-player.svg', include_in_schema=False)
async def hero_player():
    return FileResponse(Path(__file__).resolve().parent / 'assets' / 'kuponanalizii-player.svg',
                        media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


@app.get('/assets/kuponanalizii-stadium.webp', include_in_schema=False)
async def hero_stadium():
    return FileResponse(Path(__file__).resolve().parent / 'assets' / 'kuponanalizii-stadium.webp',
                        media_type='image/webp', headers={'Cache-Control': 'public, max-age=86400'})


@app.get('/dashboard-home.js', include_in_schema=False)
async def home_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-home.js'), media_type='application/javascript')


@app.get('/dashboard-results.js', include_in_schema=False)
async def results_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-results.js'), media_type='application/javascript')


@app.get('/dashboard.js', include_in_schema=False)
async def dashboard_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard.js'), media_type='application/javascript')


@app.get('/dashboard-fixtures.js', include_in_schema=False)
async def fixture_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-fixtures.js'), media_type='application/javascript')

@app.get('/dashboard-odds.js', include_in_schema=False)
async def dashboard_odds_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-odds.js'), media_type='application/javascript')

@app.get('/dashboard-predictions.js', include_in_schema=False)
async def dashboard_predictions_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-predictions.js'), media_type='application/javascript')

from src.api.routes import router
app.include_router(router, prefix='/api')


@app.get('/dashboard-market.js',include_in_schema=False)
async def market_script():return FileResponse(Path(__file__).resolve().with_name('dashboard-market.js'),media_type='application/javascript')

@app.get('/dashboard-market.css',include_in_schema=False)
async def market_styles():return FileResponse(Path(__file__).resolve().with_name('dashboard-market.css'),media_type='text/css')


@app.get('/dashboard-monochrome.css', include_in_schema=False)
async def monochrome_styles():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-monochrome.css'), media_type='text/css')


@app.get('/dashboard-coupon.js', include_in_schema=False)
async def coupon_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-coupon.js'), media_type='application/javascript')

from src.api.learning import router as learning_router
app.include_router(learning_router, prefix="/api")


@app.get('/dashboard-learning.js', include_in_schema=False)
async def learning_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-learning.js'), media_type='application/javascript')


@app.get('/dashboard-analysis.js', include_in_schema=False)
async def match_analysis_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-analysis.js'), media_type='application/javascript')


@app.get('/dashboard-revision.js',include_in_schema=False)
async def revision_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-revision.js'),media_type='application/javascript')


@app.get('/dashboard-audit.js', include_in_schema=False)
async def dashboard_audit_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-audit.js'), media_type='application/javascript')


@app.get('/dashboard-evidence.js', include_in_schema=False)
async def evidence_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard-evidence.js'), media_type='application/javascript')
