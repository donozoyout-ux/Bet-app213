"""
Scrapes match statistics from SofaScore using Playwright.

The site loads data via an XHR call. We intercept that XHR, extract the
required fields and return a DataFrame.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Dict, List, Optional

import aiosqlite
import httpx
import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

CACHE_DB = "sofascore_cache.sqlite"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


async def _init_cache() -> None:
    """Initialize the SQLite cache DB if it does not exist."""
    async with aiosqlite.connect(CACHE_DB) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key INTEGER PRIMARY KEY,
                json TEXT,
                ts INTEGER
            );"""
        )
        await db.commit()


def _extract_stats(payload: Dict) -> Dict:
    """Extract required statistics from raw payload."""
    try:
        stats = payload["statistics"]
        return {
            "match_id": payload["id"],
            "home_team": payload["homeTeam"]["name"],
            "away_team": payload["awayTeam"]["name"],
            "home_possession": float(stats["possession"]["home"]),
            "away_possession": float(stats["possession"]["away"]),
            "home_shots": int(stats["shots"]["home"]),
            "away_shots": int(stats["shots"]["away"]),
            "home_xg": float(stats["xG"]["home"]),
            "away_xg": float(stats["xG"]["away"]),
            "home_lineup_count": int(stats["lineup"]["home"]),
            "away_lineup_count": int(stats["lineup"]["away"]),
        }
    except (KeyError, TypeError) as e:
        raise ValueError(f"JSON payload missing required fields: {e}") from e


async def fetch_sofascore_stats(
    match_ids: List[int],
    *,
    timeout: int = 12,
    cache_ttl_hours: int = 12,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> pd.DataFrame:
    """Fetch statistics for many match IDs, applying caching and rate‑limit."""
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)

    await _init_cache()
    results: List[Dict] = []

    async def fetch_one(mid: int) -> None:
        cache_key = mid

        async with aiosqlite.connect(CACHE_DB) as db:
            async with db.execute(
                "SELECT json, ts FROM cache WHERE key = ?", (cache_key,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    raw_json, ts = row
                    if time.time() - ts < cache_ttl_hours * 3600:
                        results.append(json.loads(raw_json))
                        return

        async with semaphore:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                # Apply stealth via the Stealth helper
                await Stealth.apply_stealth_async(page)
                await page.set_user_agent(random.choice(_USER_AGENTS))

                payload: Optional[Dict] = None

                async def on_response(resp):
                    nonlocal payload
                    if "event/" in str(resp.url) and resp.status == 200:
                        try:
                            payload = json.loads(await resp.text())
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(
                    f"https://www.sofascore.com/match/football/{mid}",
                    wait_until="networkidle",
                    timeout=timeout * 1000,
                )
                await asyncio.sleep(random.uniform(2, 5))
                await browser.close()

                if payload is None:
                    raise httpx.HTTPStatusError(
                        f"No JSON payload captured for match {mid}",
                        request=None,
                        response=None,
                    )

                async with aiosqlite.connect(CACHE_DB) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO cache (key, json, ts) VALUES (?,?,?)",
                        (cache_key, json.dumps(payload), int(time.time())),
                    )
                    await db.commit()

                results.append(_extract_stats(payload))

    await asyncio.gather(*[fetch_one(m) for m in match_ids])

    if not results:
        return pd.DataFrame(columns=[
            "match_id", "home_team", "away_team",
            "home_possession", "away_possession",
            "home_shots", "away_shots",
            "home_xg", "away_xg",
            "home_lineup_count", "away_lineup_count",
        ])

    df = pd.DataFrame(results)
    df = df.astype({
        "match_id": "int64",
        "home_team": "object",
        "away_team": "object",
        "home_possession": "float64",
        "away_possession": "float64",
        "home_shots": "int64",
        "away_shots": "int64",
        "home_xg": "float64",
        "away_xg": "float64",
        "home_lineup_count": "int64",
        "away_lineup_count": "int64",
    })
    return df


__all__ = ["fetch_sofascore_stats", "_extract_stats"]