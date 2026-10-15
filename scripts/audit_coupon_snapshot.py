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


async def capture(client, now, folder, label):
    """Render the real API from the isolated replay DB; never contact Telegram."""
    from playwright.async_api import async_playwright,expect
    import subprocess
    folder.mkdir(parents=True,exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        try:
            for width,height in [(1920,1080),(2560,1440)]:
                page=await browser.new_page(viewport=dict(width=width,height=height),device_scale_factor=1,reduced_motion='reduce')
                await page.clock.install(time=now)
                async def forward(route):
                    path=route.request.url.removeprefix('http://audit')
                    assert route.request.url.startswith('http://audit/')
                    assert not ('telegram' in path and route.request.method=='POST')
                    if label=='before' and path in ('/','/dashboard-market.css','/dashboard-coupon.js'):
                        filename='dashboard.html' if path=='/' else path.lstrip('/')
                        body=subprocess.check_output(['git','show','origin/master:src/api/'+filename])
                        await route.fulfill(body=body,content_type='text/html' if path=='/' else 'text/css' if path.endswith('.css') else 'application/javascript')
                        return
                    response=await client.request(route.request.method,path,content=route.request.post_data,headers={k:v for k,v in route.request.headers.items() if k in ('content-type','x-coupon-key')})
                    await route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
                await page.route('**/*',forward)
                await page.goto('http://audit/#coupon')
                await expect(page.locator('#coupon-daily')).to_contain_text('Son güncelleme:' if label=='after' else 'kombine oran')
                await page.wait_for_load_state('networkidle')
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth && visualViewport.scale===1')
                await page.screenshot(path=str(folder/f'actual-{label}-{width}x{height}.png'),full_page=True)
                await page.unroute_all(behavior='ignoreErrors')
                await page.close()
        finally:await browser.close()


async def replay(snapshot, screenshots=None, label='after'):
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
                    if screenshots:await capture(client,now,screenshots,label)
                coverage = response.json()
                coverage.pop('engine_analysis', None)
                cache = coverage.pop('cache', {})
                async with db.session() as current:
                    coupon = await automatic(current, now)
                    picks = coupon.payload if coupon else None
                    diagnostics=current.info.get('coupon_diagnostics',{})
                return dict(captured_at=now.isoformat(), database='postgresql' if schema else 'sqlite',
                    http_status=response.status_code, coverage_seconds=round(time.perf_counter()-started, 4),
                    coverage=coverage, analysis_stale_leagues=cache.get('stale_leagues'),
                    automatic_coupon=picks, diagnostics=diagnostics)
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
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--label',choices=('before','after'),default='after')
    args = parser.parse_args()
    import gzip
    raw=gzip.decompress(args.snapshot.read_bytes()).decode('utf-8') if args.snapshot.suffix=='.gz' else args.snapshot.read_text(encoding='utf-8')
    result = asyncio.run(replay(json.loads(raw),args.screenshots,args.label))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('captured_at', 'database', 'http_status', 'coverage_seconds')}))
