"""BetApp213 HTTP application. Scraping is performed by the durable job worker."""
from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from src.config import settings
from src.db import database

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

app = FastAPI(title='BetApp213', version='2.0.0', lifespan=lifespan)
# Gunicorn's module-only target resolves `application`; Uvicorn continues using :app.
application = app

@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    logging.getLogger(__name__).error('Database request failed', exc_info=exc)
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable. Check server configuration.'})

@app.get('/health')
async def health():
    return {'status': 'ok', 'service': 'BetApp213'}

@app.get('/', include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).resolve().with_name('dashboard.html'))

@app.get('/dashboard.js', include_in_schema=False)
async def dashboard_script():
    return FileResponse(Path(__file__).resolve().with_name('dashboard.js'), media_type='application/javascript')

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
