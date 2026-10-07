import re
from .client import SourceError


def discover_rounds(payload):
    schedule = payload.get('ScheduleList')
    if not isinstance(schedule, dict):
        raise SourceError('ScheduleList schema changed')
    rounds = sorted(int(k[2:]) for k in schedule if re.fullmatch(r'R_\d+', k))
    if not rounds:
        raise SourceError('No league rounds found')
    return rounds
