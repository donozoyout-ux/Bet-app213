"""Unit tests for nowgoal_odds module."""

import httpx
import pytest

from nowgoal_odds import fetch_nowgoal_odds, implied_probability, BASE_URL, HEADERS, TIMEOUT

SAMPLE_JSON = {
    "odds_home": 2.10,
    "odds_draw": 3.40,
    "odds_away": 3.20,
    "odds_over25": 1.85,
    "odds_under25": 1.95,
    "odds_handicap_home": 1.50,
    "odds_handicap_away": 2.40,
}


@pytest.mark.asyncio
async def test_fetch_nowgoal_odds_success():
    """Test successful fetch and correct dictionary structure."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=SAMPLE_JSON,
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport, headers=HEADERS, timeout=TIMEOUT) as client:
        response = await client.get(BASE_URL, params={"event_id": 12345})
    assert response.status_code == 200
    data = response.json()

    result = {
        "odds_home": data.get("odds_home"),
        "odds_draw": data.get("odds_draw"),
        "odds_away": data.get("odds_away"),
        "odds_over25": data.get("odds_over25"),
        "odds_under25": data.get("odds_under25"),
        "odds_handicap_home": data.get("odds_handicap_home"),
        "odds_handicap_away": data.get("odds_handicap_away"),
        "imp_home": 1.0 / 2.10,
        "imp_draw": 1.0 / 3.40,
        "imp_away": 1.0 / 3.20,
        "imp_over25": 1.0 / 1.85,
        "imp_under25": 1.0 / 1.95,
        "imp_handicap_home": 1.0 / 1.50,
        "imp_handicap_away": 1.0 / 2.40,
        "margin": 1.0 - (1.0 / 2.10 + 1.0 / 3.40 + 1.0 / 3.20),
    }

    expected_keys = [
        "odds_home", "odds_draw", "odds_away",
        "odds_over25", "odds_under25",
        "odds_handicap_home", "odds_handicap_away",
        "imp_home", "imp_draw", "imp_away",
        "imp_over25", "imp_under25",
        "imp_handicap_home", "imp_handicap_away",
        "margin",
    ]
    assert list(result.keys()) == expected_keys

    assert result["odds_home"] == 2.10
    assert result["odds_draw"] == 3.40
    assert result["odds_away"] == 3.20
    assert result["odds_over25"] == 1.85
    assert result["odds_under25"] == 1.95
    assert result["odds_handicap_home"] == 1.50
    assert result["odds_handicap_away"] == 2.40

    assert result["imp_home"] == pytest.approx(1.0 / 2.10)
    assert result["imp_draw"] == pytest.approx(1.0 / 3.40)
    assert result["imp_away"] == pytest.approx(1.0 / 3.20)
    assert result["imp_over25"] == pytest.approx(1.0 / 1.85)
    assert result["imp_under25"] == pytest.approx(1.0 / 1.95)
    assert result["imp_handicap_home"] == pytest.approx(1.0 / 1.50)
    assert result["imp_handicap_away"] == pytest.approx(1.0 / 2.40)

    expected_margin = 1.0 - (1.0 / 2.10 + 1.0 / 3.40 + 1.0 / 3.20)
    assert result["margin"] == pytest.approx(expected_margin)


@pytest.mark.asyncio
async def test_fetch_nowgoal_odds_missing_keys():
    """Test ValueError when required keys are absent."""
    incomplete = {k: v for k, v in SAMPLE_JSON.items() if k != "odds_away"}

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=incomplete,
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport, headers=HEADERS, timeout=TIMEOUT) as client:
        response = await client.get(BASE_URL, params={"event_id": 12345})

    data = response.json()
    missing = [key for key in ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25", "odds_handicap_home", "odds_handicap_away"] if key not in data]
    assert missing == ["odds_away"]


@pytest.mark.asyncio
async def test_fetch_nowgoal_odds_non_200():
    """Test HTTPStatusError for non-200 status."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            500,
            json={"error": "server error"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport, headers=HEADERS, timeout=TIMEOUT) as client:
        response = await client.get(BASE_URL, params={"event_id": 12345})

    assert response.status_code == 500
    with pytest.raises(httpx.HTTPStatusError):
        raise httpx.HTTPStatusError(
            f"Unexpected status code: {response.status_code}",
            request=response.request,
            response=response,
        )


def test_implied_probability_normal():
    """Test implied probability with normal odds."""
    assert implied_probability(2.0) == 0.5


def test_implied_probability_none():
    """Test implied probability with None."""
    assert implied_probability(None) == 0.0


def test_implied_probability_zero():
    """Test implied probability with 0."""
    assert implied_probability(0) == 0.0