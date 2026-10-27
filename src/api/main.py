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
from src.config import settings
from src.db import database

log = logging.getLogger(__name__)
BACKGROUND_RETRY_SECONDS = 10


def storage_exhausted(error):
    """SQLSTATE survives both asyncpg and psycopg/SQLAlchemy wrapping."""
    pending, seen = [error], set()
    while pending:
        value = pending.pop()
        if value is None or id(value) in seen: continue
        seen.add(id(value))
        if getattr(value, 'sqlstate', None) == '53100' or getattr(value, 'pgcode', None) == '53100':
            return True
        pending.extend(getattr(value, key, None) for key in ('orig', '__cause__', '__context__'))
    return False


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
        except Exception as exc:
            database.ready = False
            if storage_exhausted(exc):
                database.storage_blocked = True
                log.error('[BACKGROUND] database storage exhausted (53100); automatic initialization and workers paused until reviewed recovery/restart')
                # A retry cannot create disk space. Stay cancellable, with no SQL,
                # migrations, enqueue, provider calls, or rapid restart loop.
                await asyncio.Event().wait()
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
    from src.archive.history_reader import close_reader
    await close_reader()

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


@app.get('/ready')
async def readiness():
    """Explicit DB/schema readiness; /health remains process liveness."""
    if getattr(database, 'storage_blocked', False):
        return JSONResponse(status_code=503, content={'status':'not_ready', 'reason':'database_storage_full'})
    if not database.ready:
        return JSONResponse(status_code=503, content={'status':'not_ready', 'reason':'database_not_initialized'})
    try:
        from sqlalchemy import text
        async with asyncio.timeout(2):
            async with database.engine.connect() as conn:
                if conn.dialect.name == 'postgresql':
                    present = await conn.scalar(text("SELECT to_regclass('public.prediction_publications') IS NOT NULL"))
                else:
                    present = await conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='prediction_publications'"))
        if not present:
            return JSONResponse(status_code=503, content={'status':'not_ready', 'reason':'publication_schema_missing'})
    except Exception:
        return JSONResponse(status_code=503, content={'status':'not_ready', 'reason':'database_unavailable'})
    return {'status':'ready', 'publication_schema_verified':True}

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
