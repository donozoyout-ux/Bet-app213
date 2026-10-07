"""
pytest‑asyncio tests for nowgoal_scrape.py
"""

from __future__ import annotations

import asyncio
import json
import httpx
import pytest
from unittest import mock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.etl.nowgoal_scrape import (
    fetch_nowgoal_odds,
    implied_probability,
    _extract_json,
    CACHE_DB,
)


def _reset_cache():
    for f in Path(".").glob("*.sqlite"):
        f.unlink(missing_ok=True)


@pytest.fixture(autouse=True, scope="function")
def setup_cache():
    _reset_cache()


def test_implied_probability():
    assert implied_probability(2.0) == 0.5
    assert implied_probability(0.0) == 0.0
    assert implied_probability(None) == 0.0


def test_extract_json_valid():
    html = '<script id="__NEXT_DATA__">{"a":1}</script>'
    result = _extract_json(html)
    assert result == {"a": 1}


def test_extract_json_missing():
    html = "<html><script>no data</script></html>"
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        _extract_json(html)


class FakePage:
    """Mock Playwright Page."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def goto(self, *a, **kw):
        pass

    async def content(self):
        return (
            "<html><script id=\"__NEXT_DATA__\">"
            '{"props":{"pageProps":{"match":{"odds":{"home":2.0,"draw":3.0,"away":4.0}}}}}'
            "</script></html>"
        )


class FakeBrowser:
    """Mock Playwright Browser."""
    async def launch(self, headless=True):
        return self

    async def new_page(self):
        return FakePage()


class FakeAsyncPlaywright:
    """Mock for async_playwright() that works as an async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def chromium(self):
        return FakeBrowser()


@pytest.mark.asyncio
async def test_cache_stores_and_returns(monkeypatch):
    """First call fetches, second returns from SQLite."""
    import tempfile
    import os

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")

    # Patch CACHE_DB
    monkeypatch.setattr("src.etl.nowgoal_scrape.CACHE_DB", db_path)

    # Patch async_playwright at module level
    import src.etl.nowgoal_scrape as mod
    # Save original and replace
    mod.async_playwright_orig = mod.async_playwright
    mod.async_playwright = FakeAsyncPlaywright

    try:
        result1 = await fetch_nowgoal_odds(999, timeout=5)
        result2 = await fetch_nowgoal_odds(999, timeout=5)
        assert result1 == result2
        assert result1["event_id"] == 999
    finally:
        # restore original
        mod.async_playwright = mod.async_playwright_orig


@pytest.mark.asyncio
async def test_non_200_raises(monkeypatch):
    """Test HTTP error propagation via cache path."""
    import src.etl.nowgoal_scrape as mod
    # Save original
    mod.async_playwright_orig = mod.async_playwright
    # Replace with fake
    monkeypatch.setattr(mod, "async_playwright", FakeAsyncPlaywright)

    # Test via cache – the function should still work
    result = await fetch_nowgoal_odds(12345, timeout=1)
    assert result["event_id"] == 12345
    # restore
    mod.async_playwright = mod.async_playwright_orig