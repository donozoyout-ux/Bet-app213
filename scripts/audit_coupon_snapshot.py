"""Replay an exported PostgreSQL quote snapshot in an isolated database; no sends.

Production exports are obtained with read-only SQL. TEST_POSTGRES_URL optionally
uses a fresh temporary schema; otherwise a temporary SQLite database is used.
Never pass production credentials or a production database to this script.
"""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import DateTime, insert
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from src.db import Database
from src.models import Base


async def replay(snapshot):
    from src.api.main import app
    import src.api.routes as routes
    from src.analytics.saved_coupons import automatic
    now = datetime.fromisoformat(snapshot['captured_at'].replace('Z', '+00:00'))
    with tempfile.TemporaryDirectory(prefix='betapp-coupon-audit-') as tmp:
        url = os.getenv('TEST_POSTGRES_URL') or 'sqlite+aiosqlite:///' + str(Path(tmp) / 'snapshot.db')
        db = Database(url)
        schema = 'coupon_audit_' + uuid.uuid4().hex if url.startswith('postgres') else None
        try:
            if schema:
                async with db.engine.begin() as conn:
                    await conn.execute(CreateSchema(schema))
                db.engine = db.engine.execution_options(schema_translate_map={None: schema})
                db.sessions = async_sessionmaker(db.engine, expire_on_commit=False)
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                for table in Base.metadata.sorted_tables:
                    rows = snapshot.get(table.name) or []
                    cleaned = []
                    for row in rows:
                        values = {c.name: row[c.name] for c in table.columns if c.name in row}
                        for c in table.columns:
                            if isinstance(c.type, DateTime) and isinstance(values.get(c.name), str):
                                values[c.name] = datetime.fromisoformat(values[c.name].replace('Z', '+00:00'))
                        cleaned.append(values)
                    if cleaned:
                        await conn.execute(insert(table), cleaned)
            db.ready = True
            async def session():
                async with db.session() as current:
                    yield current
            prior_db, prior_now = routes.database, routes.utcnow
            routes.database, routes.utcnow = db, lambda: now
            app.dependency_overrides[routes.session_dependency] = session
            try:
                started = time.perf_counter()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                    response = await client.get('/api/odds-coverage')
                coverage = response.json()
                coverage.pop('engine_analysis', None)
                cache = coverage.pop('cache', {})
                async with db.session() as current:
                    coupon = await automatic(current, now)
                    picks = coupon.payload if coupon else None
                return dict(captured_at=now.isoformat(), database='postgresql' if schema else 'sqlite',
                    http_status=response.status_code, coverage_seconds=round(time.perf_counter()-started, 4),
                    coverage=coverage, analysis_stale_leagues=cache.get('stale_leagues'),
                    automatic_coupon=picks)
            finally:
                routes.database, routes.utcnow = prior_db, prior_now
                app.dependency_overrides.clear()
        finally:
            if schema:
                async with db.engine.begin() as conn:
                    await conn.execute(DropSchema(schema, cascade=True, if_exists=True))
            await db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(replay(json.loads(args.snapshot.read_text(encoding='utf-8'))))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('captured_at', 'database', 'http_status', 'coverage_seconds')}))
