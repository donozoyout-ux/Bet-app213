"""sofascore_stats: Fetch match‑level statistics from the public SofaScore API.

SofaScore provides free‑tier JSON data for football/sports events.  Responses
are cached in a local SQLite database so that repeated calls for the same match
id do not exceed the rate limit and are faster.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Dict, Any

import httpx
import pandas as pd

__all__ = ["get_sofascore_stats"]

HEADERS = {
    "User-Agent": "LiveBetML/1.0 (+https://github.com/yourname/livebetml)",
    "Accept": "application/json",
}

BASE_URL = "https://www.sofascore.com/api/v1/event/{match_id}"

CACHE_DB = "sofa_cache.db"

# All fields that must be present in the API payload for a successful extraction.
REQUIRED_STATS = [
    "home_team",
    "away_team",
    "home_possession",
    "away_possession",
    "home_shots",
    "away_shots",
    "home_xg",
    "away_xg",
    "home_lineup_count",
    "away_lineup_count",
]


def _init_cache(db_path: str = CACHE_DB) -> None:
    """Create the cache table if it does not exist."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sofa_cache (
            id INTEGER PRIMARY KEY,
            json TEXT,
            fetched_at TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def _get_cached_json(db_path: str, match_id: int) -> str | None:
    """Return cached JSON if it is younger than 12 hours, otherwise None."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT json, fetched_at FROM sofa_cache WHERE id = ?",
        (match_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    json_text, fetched_at_iso = row
    try:
        fetched_ts = time.fromisoformat(fetched_at_iso).timestamp()
    except Exception:  # pragma: no cover
        return None
    age = time.time() - fetched_ts
    if age < 12 * 3600:
        return json_text
    return None


def _store_cache(db_path: str, match_id: int, json_text: str) -> None:
    """Store API response JSON in cache with the current UTC timestamp."""
    conn = sqlite3.connect(db_path)
    fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    conn.execute(
        "INSERT OR REPLACE INTO sofa_cache (id, json, fetched_at) VALUES (?, ?, ?)",
        (match_id, json_text, fetched_at),
    )
    conn.commit()
    conn.close()


def _extract_stats(payload: dict) -> dict[str, Any]:
    """Extract the required statistics from a SofaScore event payload.

    Returns a dictionary with all columns expected by ``get_sofascore_stats``.
    Raises ``ValueError`` if any required key is missing from *payload*.
    """
    missing = [k for k in REQUIRED_STATS if k not in payload]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    extracted: dict[str, Any] = {
        "home_team": payload["home_team"],
        "away_team": payload["away_team"],
        "home_possession": float(payload["home_possession"]),
        "away_possession": float(payload["away_possession"]),
        "home_shots": int(payload["home_shots"]),
        "away_shots": int(payload["away_shots"]),
        "home_xg": float(payload["home_xg"]),
        "away_xg": float(payload["away_xg"]),
        "home_lineup_count": int(payload["home_lineup_count"]),
        "away_lineup_count": int(payload["away_lineup_count"]),
    }
    return extracted


def get_sofascore_stats(match_ids: List[int]) -> pd.DataFrame:
    """Fetch match‑level statistics from the public SofaScore API.

    Parameters
    ----------
    match_ids : List[int]
        List of SofaScore match identifiers.

    Returns
    -------
    pd.DataFrame
        Columns: match_id, home_team, away_team, home_possession, away_possession,
        home_shots, away_shots, home_xg, away_xg, home_lineup_count, away_lineup_count.

    Raises
    ------
    httpx.HTTPStatusError
        If any request returns a non‑2xx status code.
    ValueError
        If a required field is missing from the API response.
    """
    _init_cache()
    rows: list[dict[str, Any]] = []

    for match_id in match_ids:
        # ----- cache lookup -----
        cached = _get_cached_json(CACHE_DB, match_id)
        if cached is not None:
            payload: dict = json.loads(cached)
        else:
            # ----- live API request -----
            resp = httpx.get(
                BASE_URL.format(match_id=match_id),
                headers=HEADERS,
                timeout=httpx.Timeout(10.0),
            )
            if resp.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"Unexpected status {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            payload = resp.json()
            _store_cache(CACHE_DB, match_id, resp.text)

        # ----- extract statistics -----
        stats = _extract_stats(payload)
        stats["match_id"] = match_id
        rows.append(stats)

    df = pd.DataFrame(rows, columns=[
        "match_id",
        "home_team",
        "away_team",
        "home_possession",
        "away_possession",
        "home_shots",
        "away_shots",
        "home_xg",
        "away_xg",
        "home_lineup_count",
        "away_lineup_count",
    ])

    # Ensure correct dtypes
    df["match_id"] = df["match_id"].astype(int)
    df["home_team"] = df["home_team"].astype(str)
    df["away_team"] = df["away_team"].astype(str)
    df["home_possession"] = df["home_possession"].astype(float)
    df["away_possession"] = df["away_possession"].astype(float)
    df["home_shots"] = df["home_shots"].astype(int)
    df["away_shots"] = df["away_shots"].astype(int)
    df["home_xg"] = df["home_xg"].astype(float)
    df["away_xg"] = df["away_xg"].astype(float)
    df["home_lineup_count"] = df["home_lineup_count"].astype(int)
    df["away_lineup_count"] = df["away_lineup_count"].astype(int)

    return df


if __name__ == "__main__":
    """Simple example: fetch stats for two dummy match IDs and print the DataFrame."""
    import sys

    dummy_ids = [1111111, 2222222]
    try:
        df = get_sofascore_stats(dummy_ids)
        print(df.to_string(index=False))
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)