"""
Scrapes odds from a NowGoal match page using Playwright (headless Chromium) and
returns a flat dictionary with normalized odds, implied probabilities and
bookmaker margin.

The page renders its data via JavaScript, so Playwright + playwright‑stealth is
required to avoid bot detection.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Dict, Optional

import aiosqlite
import httpx
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


CACHE_DB = "nowgoal_cache.sqlite"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]


async def _init_cache() -> None:
    """Initialize the SQLite cache DB if it does not exist."""
    async with aiosqlite.connect(CACHE_DB) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                html TEXT,
                ts INTEGER
            );"""
        )
        await db.commit()


def implied_probability(odds: Optional[float]) -> float:
    """Convert odds to implied probability safely."""
    if odds is None or odds == 0:
        return 0.0
    return 1.0 / odds


def _extract_json(html: str) -> Dict:
    """Find the <script id="__NEXT_DATA__"> tag and parse JSON."""
    pattern = r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.S)
    if match:
        return json.loads(match.group(1))
    raise ValueError("Unable to locate __NEXT_DATA__ script")


def _normalize(payload: Dict, event_id: int) -> Dict:
    """Convert raw JSON from __NEXT_DATA__ into the final flat dict."""
    try:
        odds = payload["props"]["pageProps"]["match"]["odds"]
    except (KeyError, TypeError):
        raise ValueError(f"Odds block missing for event {event_id}")

    def to_float(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    result = {
        "event_id": event_id,
        "odds_home": to_float(odds.get("home")),
        "odds_draw": to_float(odds.get("draw")),
        "odds_away": to_float(odds.get("away")),
        "odds_over25": to_float(odds.get("over25")),
        "odds_under25": to_float(odds.get("under25")),
        "odds_handicap_home": to_float(odds.get("handicap_home")),
        "odds_handicap_away": to_float(odds.get("handicap_away")),
    }

    for k in ["odds_home", "odds_draw", "odds_away"]:
        kp = k.replace("odds_", "imp_")
        result[kp] = implied_probability(result[k])

    result["margin"] = 1.0 - sum(
        result.get(p) for p in ["imp_home", "imp_draw", "imp_away"]
    )
    return result


class FakeAsyncPlaywright:
    """Mock for async_playwright() that works as an async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @property
    def chromium(self):
        class _Browser:
            async def launch(self, headless=True):
                pass

        return _Browser()


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


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
]


async def fetch_nowgoal_odds(
    event_id: int,
    *,
    timeout: int = 15,
    cache_ttl_hours: int = 12,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict:
    """Scrape NowGoal odds for a given event_id."""
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)

    await _init_cache()
    cache_key = str(event_id)

    async with semaphore:
        # ----- Cache check -----
        async with aiosqlite.connect(CACHE_DB) as db:
            async with db.execute(
                "SELECT html, ts FROM cache WHERE key = ?", (cache_key,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    html, ts = row
                    if time.time() - ts < cache_ttl_hours * 3600:
                        return _normalize(json.loads(html), event_id)

        # ----- Live fetch -----
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            # Apply stealth via the Stealth helper
            await Stealth.apply_stealth_async(page)
            await page.set_user_agent(random.choice(_USER_AGENTS))
            await page.goto(
                f"https://www.nowgoal.com/match/{event_id}",
                wait_until="networkidle",
                timeout=timeout * 1000,
            )
            await asyncio.sleep(random.uniform(2, 5))
            html = await page.content()
            await browser.close()

        # store in cache
        async with aiosqlite.connect(CACHE_DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO cache (key, html, ts) VALUES (?,?,?)",
                (cache_key, html, int(time.time())),
            )
            await db.commit()

        return _normalize(json.loads(html), event_id)


__all__ = ["fetch_nowgoal_odds", "implied_probability"]