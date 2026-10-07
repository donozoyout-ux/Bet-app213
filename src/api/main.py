"""
LiveBetML FastAPI application for Render.

ETL modules are intentionally imported lazily so a corrupted scraper source
cannot prevent the web service from booting.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI(title="LiveBetML API", version="1.0.2")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "livebetml"}


@app.get("/", response_class=HTMLResponse)
async def root():
    dashboard = Path(__file__).with_name("dashboard.html")
    return HTMLResponse(dashboard.read_text(encoding="utf-8"))


@app.get("/diagnostics/source")
async def source_diagnostics():
    path = Path(__file__).resolve().parents[1] / "etl" / "nowgoal_clean.py"
    data = path.read_bytes()
    return {
        "path": str(path),
        "size": len(data),
        "null_bytes": data.count(b"\x00"),
        "starts_with": data[:40].hex(),
    }


@app.get("/odds/{event_id}")
async def odds(event_id: int):
    try:
        from src.etl.nowgoal_clean import fetch_nowgoal_odds
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"NowGoal module import failed: {type(exc).__name__}: {exc}",
        ) from exc
    return await fetch_nowgoal_odds(event_id)


@app.get("/sofascore/{match_id}")
async def sofascore(match_id: int):
    try:
        from src.etl.sofascore_scrape import fetch_sofascore_stats
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"SofaScore module import failed: {type(exc).__name__}: {exc}",
        ) from exc
    df = await fetch_sofascore_stats([match_id])
    return df.to_dict(orient="records")
