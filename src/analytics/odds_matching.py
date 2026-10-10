"""Verified bookmaker odds matching for prediction candidates and recommendations.

Supports Crown, Bet365 and Sbobet.
Matches odds by:
- Match ID
- Market family (result, goals, asian_handicap)
- Selection (home/draw/away, over/under, handicap side)
- Exact goal/handicap line
- Period (full_time, first_half, second_half) — 1H/2H never substitutes full-time
- Bookmaker (Crown, Bet365, Sbobet)
- Price stage (opening, latest, closing)
"""
import math
from datetime import datetime, timezone
from typing import Any, Mapping

SUPPORTED_BOOKMAKERS = ('Crown', 'Bet365', 'Sbobet')
NO_ODDS_MESSAGE = 'Bu market için doğrulanmış oran yok'


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _unpack_row(item: Any) -> tuple[Any, str | None]:
    if hasattr(item, '__getitem__'):
        try:
            return item[0], item[1]
        except (IndexError, TypeError, KeyError):
            pass
    return item, getattr(item, 'bookmaker', None)


def parse_market_and_selection(market: str, selection: str) -> tuple[str, str, float | None, str]:
    """Parse candidate market and selection into (family, side, line, period).

    Returns:
        family: 'result' | 'goals' | 'asian_handicap' | 'first_half_goals' | 'second_half_goals' | 'corners' | 'cards' | other
        side: 'home' | 'draw' | 'away' | 'over' | 'under' | ''
        line: float or None
        period: 'full_time' | 'first_half' | 'second_half'
    """
    if market == 'result':
        return ('result', selection, None, 'full_time')
    elif market == 'goals':
        # selection is e.g. 'over_2_5', 'under_1_5'
        parts = selection.split('_', 1)
        side = parts[0]
        line = float(parts[1].replace('_', '.')) if len(parts) > 1 else None
        return ('goals', side, line, 'full_time')
    elif market == 'asian_handicap':
        # selection is e.g. 'home_minus_0_5', 'away_plus_0_5', 'home_plus_0'
        if '_plus_' in selection:
            side, line_str = selection.split('_plus_')
            signed = float(line_str.replace('_', '.'))
        elif '_minus_' in selection:
            side, line_str = selection.split('_minus_')
            signed = -float(line_str.replace('_', '.'))
        else:
            side = 'home' if selection.startswith('home') else 'away'
            signed = 0.0
        # For handicap in AsianHandicap table, the line is home-perspective:
        # If side == 'home', table line is signed
        # If side == 'away', table line is -signed
        required_home_line = signed if side == 'home' else -signed
        return ('asian_handicap', side, required_home_line, 'full_time')
    elif market == 'first_half_goals':
        parts = selection.replace('_first_half_goals', '').split('_', 1)
        side = parts[0] if parts else ''
        line = float(parts[1].replace('_', '.')) if len(parts) > 1 else None
        return ('first_half_goals', side, line, 'first_half')
    elif market == 'second_half_goals':
        parts = selection.replace('_second_half_goals', '').split('_', 1)
        side = parts[0] if parts else ''
        line = float(parts[1].replace('_', '.')) if len(parts) > 1 else None
        return ('second_half_goals', side, line, 'second_half')
    elif market in ('corners', 'cards'):
        # selection like 'over_9_5' or 'under_4_5'
        parts = selection.split('_', 1)
        side = parts[0] if parts else ''
        line = float(parts[1].replace('_', '.')) if len(parts) > 1 else None
        return (market, side, line, 'full_time')
    else:
        return (market, selection, None, 'full_time')


def _is_valid_odds(val: Any) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return math.isfinite(f) and f > 1.0
    except (TypeError, ValueError):
        return False


def _safe_float(val: Any) -> float | None:
    if _is_valid_odds(val):
        return round(float(val), 4)
    return None


def match_odds_for_candidate(
    candidate: Mapping[str, Any],
    match_id: int,
    match_status: str,
    odds_1x2_rows: list[Any] | None = None,
    asian_totals_rows: list[Any] | None = None,
    asian_handicap_rows: list[Any] | None = None,
    period_odds_rows: list[Any] | None = None,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Find verified odds across Crown, Bet365, Sbobet for a specific candidate.

    Never substitutes full-time odds for first-half or second-half markets.
    Never invents odds if no exact line/market match exists.
    """
    market = candidate.get('market', '')
    selection = candidate.get('selection', '')
    probability = candidate.get('probability')

    family, side, line, period = parse_market_and_selection(market, selection)

    # Price stage determination
    is_closing = match_status == 'finished'
    stage = 'closing' if is_closing else 'latest'

    # If the period is first_half or second_half, we MUST NOT use full-time tables
    if period in ('first_half', 'second_half'):
        # Only use period_odds_rows if explicitly provided and tagged with matching period
        rows_to_check = period_odds_rows or []
        # If no period-specific verified odds exist, report no verified odds
        if not rows_to_check:
            return {
                'has_odds': False,
                'period': period,
                'stage': stage,
                'bookmakers': {},
                'best_price': None,
                'best_bookmaker': None,
                'best_implied_probability': None,
                'best_value': None,
                'observed_at': None,
                'message': NO_ODDS_MESSAGE,
            }

    # If market has no supported bookmaker odds table (e.g. btts, double_chance)
    if family not in ('result', 'goals', 'asian_handicap', 'corners', 'cards'):
        return {
            'has_odds': False,
            'period': period,
            'stage': stage,
            'bookmakers': {},
            'best_price': None,
            'best_bookmaker': None,
            'best_implied_probability': None,
            'best_value': None,
            'observed_at': None,
            'message': NO_ODDS_MESSAGE,
        }

    bookmakers_data: dict[str, dict[str, Any]] = {}
    valid_prices: list[tuple[str, float, datetime | None]] = []

    if family == 'result':
        rows = odds_1x2_rows or []
        for item in rows:
            row, bookmaker_name = _unpack_row(item)
            if bookmaker_name not in SUPPORTED_BOOKMAKERS:
                continue

            updated_at = getattr(row, 'updated_at', None)
            if cutoff and updated_at and _aware(updated_at) > _aware(cutoff):
                continue

            opening = _safe_float(getattr(row, f'opening_{side}', None))
            latest = _safe_float(getattr(row, f'latest_{side}', None))
            closing = _safe_float(getattr(row, f'closing_{side}', None))

            # Current price for the active stage
            current = closing if (is_closing and closing is not None) else (latest if latest is not None else opening)

            implied = round(1.0 / current, 4) if current else None
            val = round(probability * current - 1.0, 4) if (current and probability is not None) else None

            entry = {
                'bookmaker': bookmaker_name,
                'opening_odds': opening,
                'latest_odds': latest,
                'closing_odds': closing,
                'current_odds': current,
                'implied_probability': implied,
                'value': val,
                'observed_at': updated_at.isoformat() if hasattr(updated_at, 'isoformat') else str(updated_at) if updated_at else None,
            }
            bookmakers_data[bookmaker_name] = entry
            if current:
                valid_prices.append((bookmaker_name, current, updated_at))

    elif family == 'goals':
        # exact total line required
        rows = asian_totals_rows or []
        for item in rows:
            row, bookmaker_name = _unpack_row(item)
            if bookmaker_name not in SUPPORTED_BOOKMAKERS:
                continue

            updated_at = getattr(row, 'updated_at', None)
            if cutoff and updated_at and _aware(updated_at) > _aware(cutoff):
                continue

            op_line = getattr(row, 'opening_line', None)
            lat_line = getattr(row, 'latest_line', None)
            cl_line = getattr(row, 'closing_line', None)

            opening = None
            if op_line is not None and line is not None and abs(op_line - line) < 1e-4:
                opening = _safe_float(getattr(row, f'opening_{side}', None))

            latest = None
            if lat_line is not None and line is not None and abs(lat_line - line) < 1e-4:
                latest = _safe_float(getattr(row, f'latest_{side}', None))

            closing = None
            if cl_line is not None and line is not None and abs(cl_line - line) < 1e-4:
                closing = _safe_float(getattr(row, f'closing_{side}', None))

            # If none of the lines matched the target line, this row does not provide odds for this market
            if opening is None and latest is None and closing is None:
                continue

            current = closing if (is_closing and closing is not None) else (latest if latest is not None else opening)
            implied = round(1.0 / current, 4) if current else None
            val = round(probability * current - 1.0, 4) if (current and probability is not None) else None

            entry = {
                'bookmaker': bookmaker_name,
                'opening_odds': opening,
                'latest_odds': latest,
                'closing_odds': closing,
                'current_odds': current,
                'implied_probability': implied,
                'value': val,
                'observed_at': updated_at.isoformat() if hasattr(updated_at, 'isoformat') else str(updated_at) if updated_at else None,
                'line': line,
            }
            bookmakers_data[bookmaker_name] = entry
            if current:
                valid_prices.append((bookmaker_name, current, updated_at))

    elif family == 'asian_handicap':
        # exact handicap line required
        rows = asian_handicap_rows or []
        for item in rows:
            row, bookmaker_name = _unpack_row(item)
            if bookmaker_name not in SUPPORTED_BOOKMAKERS:
                continue

            updated_at = getattr(row, 'updated_at', None)
            if cutoff and updated_at and _aware(updated_at) > _aware(cutoff):
                continue

            op_line = getattr(row, 'opening_line', None)
            lat_line = getattr(row, 'latest_line', None)
            cl_line = getattr(row, 'closing_line', None)

            opening = None
            if op_line is not None and line is not None and abs(op_line - line) < 1e-4:
                opening = _safe_float(getattr(row, f'opening_{side}', None))

            latest = None
            if lat_line is not None and line is not None and abs(lat_line - line) < 1e-4:
                latest = _safe_float(getattr(row, f'latest_{side}', None))

            closing = None
            if cl_line is not None and line is not None and abs(cl_line - line) < 1e-4:
                closing = _safe_float(getattr(row, f'closing_{side}', None))

            if opening is None and latest is None and closing is None:
                continue

            current = closing if (is_closing and closing is not None) else (latest if latest is not None else opening)
            implied = round(1.0 / current, 4) if current else None
            val = round(probability * current - 1.0, 4) if (current and probability is not None) else None

            entry = {
                'bookmaker': bookmaker_name,
                'opening_odds': opening,
                'latest_odds': latest,
                'closing_odds': closing,
                'current_odds': current,
                'implied_probability': implied,
                'value': val,
                'observed_at': updated_at.isoformat() if hasattr(updated_at, 'isoformat') else str(updated_at) if updated_at else None,
                'line': line,
            }
            bookmakers_data[bookmaker_name] = entry
            if current:
                valid_prices.append((bookmaker_name, current, updated_at))

    elif family in ('corners', 'cards'):
        rows = period_odds_rows or []
        for item in rows:
            row, bookmaker_name = _unpack_row(item)
            if bookmaker_name not in SUPPORTED_BOOKMAKERS:
                continue

            updated_at = getattr(row, 'updated_at', None)
            if cutoff and updated_at and _aware(updated_at) > _aware(cutoff):
                continue

            op_line = getattr(row, 'opening_line', None)
            lat_line = getattr(row, 'latest_line', None)
            cl_line = getattr(row, 'closing_line', None)

            opening = None
            if op_line is not None and line is not None and abs(op_line - line) < 1e-4:
                opening = _safe_float(getattr(row, f'opening_{side}', None))

            latest = None
            if lat_line is not None and line is not None and abs(lat_line - line) < 1e-4:
                latest = _safe_float(getattr(row, f'latest_{side}', None))

            closing = None
            if cl_line is not None and line is not None and abs(cl_line - line) < 1e-4:
                closing = _safe_float(getattr(row, f'closing_{side}', None))

            if opening is None and latest is None and closing is None:
                continue

            current = closing if (is_closing and closing is not None) else (latest if latest is not None else opening)
            implied = round(1.0 / current, 4) if current else None
            val = round(probability * current - 1.0, 4) if (current and probability is not None) else None

            entry = {
                'bookmaker': bookmaker_name,
                'opening_odds': opening,
                'latest_odds': latest,
                'closing_odds': closing,
                'current_odds': current,
                'implied_probability': implied,
                'value': val,
                'observed_at': updated_at.isoformat() if hasattr(updated_at, 'isoformat') else str(updated_at) if updated_at else None,
                'line': line,
            }
            bookmakers_data[bookmaker_name] = entry
            if current:
                valid_prices.append((bookmaker_name, current, updated_at))

    # Evaluate best verified price
    if valid_prices:
        # Sort by price descending
        valid_prices.sort(key=lambda x: -x[1])
        best_bk, best_p, _ = valid_prices[0]
        best_implied = round(1.0 / best_p, 4)
        best_val = round(probability * best_p - 1.0, 4) if probability is not None else None

        # Find latest observed_at
        stamps = [p[2] for p in valid_prices if p[2] is not None]
        latest_stamp = max(stamps) if stamps else None
        latest_stamp_str = latest_stamp.isoformat() if hasattr(latest_stamp, 'isoformat') else str(latest_stamp) if latest_stamp else None

        return {
            'has_odds': True,
            'period': period,
            'stage': stage,
            'bookmakers': bookmakers_data,
            'best_price': best_p,
            'best_bookmaker': best_bk,
            'best_implied_probability': best_implied,
            'best_value': best_val,
            'observed_at': latest_stamp_str,
            'message': None,
        }
    else:
        return {
            'has_odds': False,
            'period': period,
            'stage': stage,
            'bookmakers': bookmakers_data,
            'best_price': None,
            'best_bookmaker': None,
            'best_implied_probability': None,
            'best_value': None,
            'observed_at': None,
            'message': NO_ODDS_MESSAGE,
        }


def enrich_recommendations_with_odds(
    recommendations: list[dict[str, Any]],
    match_id: int,
    match_status: str,
    odds_1x2_rows: list[Any] | None = None,
    asian_totals_rows: list[Any] | None = None,
    asian_handicap_rows: list[Any] | None = None,
    period_odds_rows: list[Any] | None = None,
    cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Attach verified_odds to each recommendation / candidate dict in-place."""
    for item in recommendations:
        item['verified_odds'] = match_odds_for_candidate(
            candidate=item,
            match_id=match_id,
            match_status=match_status,
            odds_1x2_rows=odds_1x2_rows,
            asian_totals_rows=asian_totals_rows,
            asian_handicap_rows=asian_handicap_rows,
            period_odds_rows=period_odds_rows,
            cutoff=cutoff,
        )
    return recommendations
