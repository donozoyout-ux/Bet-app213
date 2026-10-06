"""nowgoal_odds: Fetch and normalize odds from the NowGoal API.

The NowGoal API is a publicly documented JSON endpoint; the free tier
requires no authentication.
"""

from __future__ import annotations

import httpx

__all__ = ["fetch_nowgoal_odds", "implied_probability"]

BASE_URL = "https://api.nowgoal.com/v1/odds"
HEADERS = {
    "User-Agent": "LiveBetML/1.0 (+https://github.com/yourname/livebetml)",
    "Accept": "application/json",
}
TIMEOUT = httpx.Timeout(5.0)

REQUIRED_ODDS_KEYS = [
    "odds_home",
    "odds_draw",
    "odds_away",
    "odds_over25",
    "odds_under25",
    "odds_handicap_home",
    "odds_handicap_away",
]


def implied_probability(odds: float | None) -> float:
    """Convert decimal odds to implied probability.

    Parameters
    ----------
    odds: float | None
        Odds value, or None/0.

    Returns
    -------
    float
        Implied probability (0.0 if odds is None or 0, otherwise 1.0 / odds).
    """
    if odds is None or odds == 0:
        return 0.0
    return 1.0 / odds


async def fetch_nowgoal_odds(event_id: int) -> dict:
    """Fetch odds for a given NowGoal event ID.

    Parameters
    ----------
    event_id: int
        The NowGoal event identifier.

    Returns
    -------
    dict
        Normalised odds dictionary containing:
        - odds_home, odds_draw, odds_away
        - odds_over25, odds_under25
        - odds_handicap_home, odds_handicap_away
        - implied probabilities for every odds field
        - bookmaker margin (1 - sum(implied probabilities))

    Raises
    ------
    httpx.HTTPStatusError
        If the HTTP request returns a non‑2xx status code.
    httpx.RequestError
        For network‑level problems (timeout, DNS failure, etc.).
    ValueError
        If the response JSON is missing required keys.
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT) as client:
        response = await client.get(BASE_URL, params={"event_id": event_id})

    if response.status_code != 200:
        raise httpx.HTTPStatusError(
            f"Unexpected status code: {response.status_code}",
            request=response.request,
            response=response,
        )

    data = response.json()

    # Check for required keys
    missing = [key for key in REQUIRED_ODDS_KEYS if key not in data]
    if missing:
        raise ValueError(f"Missing required odds fields: {', '.join(missing)}")

    # Extract values (may be None if explicitly null in JSON)
    odds_home = data.get("odds_home")
    odds_draw = data.get("odds_draw")
    odds_away = data.get("odds_away")
    odds_over25 = data.get("odds_over25")
    odds_under25 = data.get("odds_under25")
    odds_handicap_home = data.get("odds_handicap_home")
    odds_handicap_away = data.get("odds_handicap_away")

    # Compute implied probabilities
    imp_home = implied_probability(odds_home)
    imp_draw = implied_probability(odds_draw)
    imp_away = implied_probability(odds_away)
    imp_over25 = implied_probability(odds_over25)
    imp_under25 = implied_probability(odds_under25)
    imp_handicap_home = implied_probability(odds_handicap_home)
    imp_handicap_away = implied_probability(odds_handicap_away)

    # Compute bookmaker margin for 1-X-2 market
    margin = 1.0 - (imp_home + imp_draw + imp_away)

    return {
        "odds_home": odds_home,
        "odds_draw": odds_draw,
        "odds_away": odds_away,
        "odds_over25": odds_over25,
        "odds_under25": odds_under25,
        "odds_handicap_home": odds_handicap_home,
        "odds_handicap_away": odds_handicap_away,
        "imp_home": imp_home,
        "imp_draw": imp_draw,
        "imp_away": imp_away,
        "imp_over25": imp_over25,
        "imp_under25": imp_under25,
        "imp_handicap_home": imp_handicap_home,
        "imp_handicap_away": imp_handicap_away,
        "margin": margin,
    }


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        result = await fetch_nowgoal_odds(12345)
        print(result)

    asyncio.run(_main())