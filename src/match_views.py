"""UTC boundaries and shared state semantics for the API and collector."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DISPLAY_TIMEZONE = 'Europe/Istanbul'
LIVE_STATUSES = ('live', 'first_half', 'half_time', 'second_half', 'extra_time', 'penalties')


def day_bounds(day, timezone_name=DISPLAY_TIMEZONE):
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def active_bounds(now):
    """Today plus THREE following Istanbul dates, ending at exclusive midnight.

    This is a product calendar window, not a 72-hour training retention policy.
    The disabled flag retains the existing seven-day prematch horizon.
    """
    from src.analytics.history_cache import enabled
    if not enabled():
        return now, now + timedelta(days=7)
    today = now.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).date()
    start, _ = day_bounds(today)
    end, _ = day_bounds(today + timedelta(days=4))
    return start, end


def upcoming_predicate(now):
    from src.models import Match
    from src.analytics.history_cache import enabled
    _, end = active_bounds(now)
    return (Match.kickoff_at > now) & (Match.kickoff_at < end if enabled() else Match.kickoff_at <= end)
