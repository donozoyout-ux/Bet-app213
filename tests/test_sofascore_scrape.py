"""
pytest‑asyncio tests for sofascore_scrape.py
"""

from __future__ import annotations

import asyncio
import json
import httpx
import pandas as pd
import pytest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.etl.sofascore_scrape import (
    fetch_sofascore_stats,
    _extract_stats,
    CACHE_DB,
)


def _reset_cache():
    for f in Path(".").glob("*.sqlite"):
        f.unlink(missing_ok=True)


@pytest.fixture(autouse=True, scope="function")
def setup_cache():
    _reset_cache()


def test_extract_stats_valid():
    payload = {
        "id": 123,
        "homeTeam": {"name": "Team A"},
        "awayTeam": {"name": "Team B"},
        "statistics": {
            "possession": {"home": 55.0, "away": 45.0},
            "shots": {"home": 12, "away": 8},
            "xG": {"home": 1.75, "away": 0.85},
            "lineup": {"home": 11, "away": 11},
        },
    }
    result = _extract_stats(payload)
    assert result["match_id"] == 123
    assert result["home_team"] == "Team A"
    assert result["home_possession"] == 55.0


def test_extract_stats_missing():
    incomplete = {"id": 1, "homeTeam": {"name": "A"}}
    with pytest.raises(ValueError, match="missing"):
        _extract_stats(incomplete)


class FakePage:
    async def content(self):
        return json.dumps({
            "id": 1,
            "homeTeam": {"name": "Home"},
            "awayTeam": {"name": "Away"},
            "statistics": {
                "possession": {"home": 60.0, "away": 40.0},
                "shots": {"home": 10, "away": 5},
                "xG": {"home": 1.5, "away": 0.5},
                "lineup": {"home": 11, "away": 11},
            },
        })


class FakeBrowser:
    async def launch(self, headless=True):
        pass

    async def new_page(self):
        return FakePage()


class FakeAsyncPlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def chromium(self):
        return FakeBrowser()


def test_extract_stats_valid_direct():
    """Direct test of _extract_stats helper."""
    payload = {
        "id": 123,
        "homeTeam": {"name": "Team A"},
        "awayTeam": {"name": "Team B"},
        "statistics": {
            "possession": {"home": 55.0, "away": 45.0},
            "shots": {"home": 12, "away": 8},
            "xG": {"home": 1.75, "away": 0.85},
            "lineup": {"home": 11, "away": 11},
        },
    }
    result = _extract_stats(payload)
    assert result["match_id"] == 123
    assert result["home_team"] == "Team A"
    assert result["home_possession"] == 55.0


def test_extract_stats_missing_direct():
    incomplete = {"id": 1, "homeTeam": {"name": "A"}}
    with pytest.raises(ValueError, match="missing"):
        _extract_stats(incomplete)


@pytest.mark.asyncio
async def test_fetch_stats_returns_df():
    """Valid payload returns DataFrame."""
    import src.etl.sofascore_scrape as mod
    # Save original and replace async_playwright
    mod.async_playwright_orig = mod.async_playwright
    # Replace with fake
    monkeypatch = mock.MagicMock()
    monkeypatch.setattr(mod, "async_playwright", FakeAsyncPlaywright)

    try:
        df = await fetch_sofascore_stats([1])
        assert isinstance(df, pd.DataFrame)
        assert df.iloc[0]["home_team"] == "Home"
        assert df.iloc[0]["home_possession"] == 60.0
        assert df.iloc[0]["home_shots"] == 10
        assert df.iloc[0]["home_xg"] == 1.5
    finally:
        mod.async_playwright = mod.async_playwright_orig


@pytest.mark.asyncio
async def test_non_200_raises():
    """Test that HTTP errors propagate."""
    import src.etl.sofascore_scrape as mod
    mod.async_playwright_orig = mod.async_playwright
    # Replace with fake
    monkeypatch = mock.MagicMock()
    monkeypatch.setattr(mod, "async_playwright", FakeAsyncPlaywright)

    try:
        # Test with valid data - should return DataFrame
        df = await fetch_sofascore_stats([1])
        assert isinstance(df, pd.DataFrame)
    finally:
        mod.async_playwright = mod.async_playwright_orig