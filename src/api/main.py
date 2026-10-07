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

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    async def initialize_and_work():
        while not database.ready:
            try:
                await database.initialize()
            except (SQLAlchemyError, OSError):
                logging.getLogger(__name__).exception('Database unavailable; retrying while health remains available')
                await asyncio.sleep(10)
        if settings.worker_enabled:
            from src.jobs.worker import work
            await work()
    # Do not make liveness/dashboard startup wait on a database connection timeout.
    worker = asyncio.create_task(initialize_and_work()) if database.configured else None
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
    return FileResponse(Path(__file__).with_name('dashboard.html'))

@app.get('/dashboard.js', include_in_schema=False)
async def dashboard_script():
    return FileResponse(Path(__file__).with_name('dashboard.js'), media_type='application/javascript')

from src.api.routes import router
app.include_router(router, prefix='/api')
