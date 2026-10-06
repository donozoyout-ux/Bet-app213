"""
LiveBetML FastAPI Application

Provides REST API endpoints for:
- NowGoal odds fetching
- SofaScore statistics retrieval
- FlashScore live event monitoring
- Model predictions
"""

from __future__ import annotations

from fastapi import FastAPI
from contextlib import asynccontextmanager

# Import our modules
from src.etl.nowgoal_scrape import fetch_nowgoal_odds, implied_probability
from src.etl.sofascore_stats import get_sofascore_stats
from src.etl.flashscore_live import watch_flashscore_match

app = FastAPI(title="LiveBetML API", version="1.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup event."""
    # Initialize caches and connections
    yield


app = FastAPI(
    title="LiveBetML API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "livebetml"}


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "LiveBetML API is running"}


# Include routers from submodules would go here