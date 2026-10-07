"""
LiveBetML FastAPI application for Render.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.etl.nowgoal_clean import fetch_nowgoal_odds, implied_probability
from src.etl.sofascore_scrape import fetch_sofascore_stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="LiveBetML API",
    version="1.0.1",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "livebetml"}


@app.get("/")
async def root():
    return {"message": "LiveBetML API is running"}


@app.get("/odds/{event_id}")
async def odds(event_id: int):
    return await fetch_nowgoal_odds(event_id)


@app.get("/sofascore/{match_id}")
async def sofascore(match_id: int):
    df = await fetch_sofascore_stats([match_id])
    return df.to_dict(orient="records")
