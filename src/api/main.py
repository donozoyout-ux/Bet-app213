"""
LiveBetML FastAPI Application

Provides REST API endpoints for:
- NowGoal odds fetching
- SofaScore statistics retrieval
- FlashScore live event monitoring
- Model predictions
"""

from __future__ import annotations

import sys
import os

# Ensure the project root directory is in sys.path so that
# 'etl.nowgoal_scrape' and 'etl.sofascore_stats' can be imported
# regardless of the working directory (especially important for Render.com)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print(f"DEBUG: __file__={__file__}, _project_root={_project_root}, sys.path={sys.path[:3]}...")
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastapi import FastAPI
from contextlib import asynccontextmanager

# Import our modules from the etl package
from etl.nowgoal_scrape import fetch_nowgoal_odds, implied_probability
from etl.sofascore_stats import get_sofascore_stats
from etl.flashscore_live import watch_flashscore_match

app = FastAPI(title="LiveBetML API", version="1.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup event - initialize caches and connections."""
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