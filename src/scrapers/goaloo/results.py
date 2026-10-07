import re

STATUSES = {-1: 'finished', 0: 'scheduled', 1: 'live', 2: 'half_time', 3: 'live',
            4: 'extra_time', 5: 'penalties', -10: 'cancelled', -11: 'pending',
            -12: 'abandoned', -13: 'interrupted', -14: 'postponed'}


def parse_score(value):
    if value in ('', None, '-'):
        return None, None
    if not isinstance(value, str) or not re.fullmatch(r'\d+\s*-\s*\d+', value):
        raise ValueError(f'Unrecognized score: {value!r}')
    return tuple(int(s.strip()) for s in value.split('-'))


def match_status(value):
    return STATUSES.get(int(value), 'unknown')
