"""Read-only four-product coupon composition from vetted, *current* candidate records.

This does not publish, persist, replace an edition, or send Telegram messages.
All product variants use the existing qualification, quote freshness and
concentration/risk policy; missing targets remain honestly unavailable.
"""
from datetime import datetime
from decimal import Decimal
from itertools import combinations
import math

from src.analytics.dynamic_coupon import select_portfolio

POLICY_VERSION = "coupon-products-readonly-v1"
BANKO_MIN = Decimal("1.70")
BANKO_MAX = Decimal("1.90")
COMBINATION_SEARCH_MAX = 12
TARGETS = {"combined_6_7": (Decimal("6"), Decimal("7")),
           "combined_8_10": (Decimal("8"), Decimal("10"))}


def _price(item):
    value = item["quote"]["price"]
    if type(value) not in (float, int, str, Decimal):
        raise ValueError("invalid_verified_price")
    try:
        price = Decimal(str(value))
    except Exception:
        raise ValueError("invalid_verified_price") from None
    if not price.is_finite() or price <= 1:
        raise ValueError("invalid_verified_price")
    return price


def _unavailable(kind, reason, now):
    return dict(kind=kind, status="unavailable", reason=reason, items=[],
                combined_odds=None, checked_at=now.isoformat(),
                policy_version=POLICY_VERSION, published=False)


def _available(kind, items, now):
    odds = Decimal(1)
    for item in items:
        odds *= _price(item)
    try:
        combined = float(odds)
    except (ValueError, OverflowError):
        return _unavailable(kind, "combined_odds_out_of_range", now)
    if not math.isfinite(combined):
        return _unavailable(kind, "combined_odds_out_of_range", now)
    return dict(kind=kind, status="candidate_only", reason=None,
                items=items, combined_odds=combined,
                checked_at=now.isoformat(), policy_version=POLICY_VERSION,
                published=False)


def _combination(kind, vetted, now, bounds):
    if not vetted:
        return _unavailable(kind, "no_qualified_candidates", now)
    if len(vetted) > COMBINATION_SEARCH_MAX:
        # Never scan an arbitrary prefix and misleadingly report no matches.
        return _unavailable(kind, "search_budget_exhausted", now)
    low, high = bounds
    midpoint = (low + high) / 2
    choices = []
    # Every selected subset inherits distinct fixtures/teams and quality checks
    # from the already-validated, risk-bounded base portfolio.
    for size in range(2, len(vetted) + 1):
        for subset in combinations(vetted, size):
            total = Decimal(1)
            for item in subset:
                total *= _price(item)
                if total > high:
                    break
            if low <= total <= high:
                signature = tuple(sorted((item["match"]["id"],
                                          item["recommendation"]["id"])
                                         for item in subset))
                choices.append((abs(total - midpoint), size, signature, subset))
    if not choices:
        return _unavailable(kind, "no_target_range_from_qualified_portfolio", now)
    _, _, _, items = min(choices, key=lambda x: x[:3])
    return _available(kind, list(items), now)


def compose_products(records, now: datetime):
    """Return candidates only; caller MUST freeze editions before publication.

    Product kinds are independent. Selecting a balanced coupon never alters the
    Banko or target-range subsets; no price is simulated or rounded for a match.
    No total-odds target is guaranteed, and none weakens evidence floors.
    """
    if now.tzinfo is None:
        raise ValueError("timezone_aware_now_required")
    records = list(records)
    selected, audit = select_portfolio(records, now, strategy="balanced")
    balanced = (_available("balanced", selected, now) if selected else
                _unavailable("balanced", "no_qualified_candidates", now))

    # Banko is not simply the first leg in an unrelated combined slip.
    near_banko = []
    for item in records:
        raw = item.get("quote", {}).get("price")
        if type(raw) not in (float, int, str, Decimal):
            continue
        try:
            price = Decimal(str(raw))
        except Exception:
            continue
        if price.is_finite() and BANKO_MIN <= price <= BANKO_MAX:
            near_banko.append(item)
    banko_items, _ = select_portfolio(near_banko, now, strategy="cautious", limit=1)
    banko = (_available("banko", banko_items, now) if banko_items else
             _unavailable("banko", "no_verified_banko_near_1_80", now))

    return dict(policy_version=POLICY_VERSION,
                banko=banko,
                balanced=balanced,
                combined_6_7=_combination("combined_6_7", selected, now, TARGETS["combined_6_7"]),
                combined_8_10=_combination("combined_8_10", selected, now, TARGETS["combined_8_10"]),
                balanced_audit=audit,
                publication_note="Read-only candidates, not published editions")
