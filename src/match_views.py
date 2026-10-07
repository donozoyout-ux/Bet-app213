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
