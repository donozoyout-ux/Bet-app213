"""Clean NowGoal scraper module used by Render."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Dict, Optional

import aiosqlite
from playwright.async_api import async_playwright

CACHE_DB = "nowgoal_cache.sqlite"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


async def _init_cache() -> None:
    async with aiosqlite.connect(CACHE_DB) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(key TEXT PRIMARY KEY, html TEXT, ts INTEGER)"
        )
        await db.commit()


def implied_probability(odds: Optional[float]) -> float:
    if odds is None or odds == 0:
        return 0.0
    return 1.0 / odds


def _extract_payload(html: str) -> Dict:
    match = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise ValueError("Unable to locate __NEXT_DATA__ script")
    return json.loads(match.group(1))


def _normalize(payload: Dict, event_id: int) -> Dict:
    try:
        odds = payload["props"]["pageProps"]["match"]["odds"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Odds block missing for event {event_id}") from exc

    def as_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    result = {
        "event_id": event_id,
        "odds_home": as_float(odds.get("home")),
        "odds_draw": as_float(odds.get("draw")),
        "odds_away": as_float(odds.get("away")),
        "odds_over25": as_float(odds.get("over25")),
        "odds_under25": as_float(odds.get("under25")),
        "odds_handicap_home": as_float(odds.get("handicap_home")),
        "odds_handicap_away": as_float(odds.get("handicap_away")),
    }

    result["imp_home"] = implied_probability(result["odds_home"])
    result["imp_draw"] = implied_probability(result["odds_draw"])
    result["imp_away"] = implied_probability(result["odds_away"])
    result["margin"] = 1.0 - (
        result["imp_home"] + result["imp_draw"] + result["imp_away"]
    )
    return result


async def fetch_nowgoal_odds(
    event_id: int,
    *,
    timeout: int = 15,
    cache_ttl_hours: int = 12,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict:
    if semaphore is None:
        semaphore = asyncio.Semaphore(1)

    await _init_cache()
    cache_key = str(event_id)

    async with semaphore:
        async with aiosqlite.connect(CACHE_DB) as db:
            async with db.execute(
                "SELECT html, ts FROM cache WHERE key = ?", (cache_key,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    html, ts = row
                    if time.time() - ts < cache_ttl_hours * 3600:
                        return _normalize(_extract_payload(html), event_id)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=random.choice(USER_AGENTS))
            page = await context.new_page()
            await page.goto(
                f"https://www.nowgoal.com/match/{event_id}",
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
            await asyncio.sleep(1.5)
            html = await page.content()
            await browser.close()

        async with aiosqlite.connect(CACHE_DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO cache (key, html, ts) VALUES (?, ?, ?)",
                (cache_key, html, int(time.time())),
            )
            await db.commit()

        return _normalize(_extract_payload(html), event_id)


__all__ = ["fetch_nowgoal_odds", "implied_probability"]
