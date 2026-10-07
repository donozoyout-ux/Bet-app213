import math


def parse_line(value):
    if value in (None, '', '-', '--'):
        return None
    text = str(value).strip()
    parts = text.split('/')
    if len(parts) > 2:
        raise ValueError('Invalid Asian line')
    values = [float(v) for v in parts]
    if len(values) == 2 and text.startswith('-') and not parts[1].startswith('-'):
        values[1] = -values[1]
    result = sum(values) / len(values)
    if not math.isfinite(result):
        raise ValueError('Invalid Asian line')
    return result


def home_handicap(value):
    """Goaloo positive line means home gives goals; expose the home perspective."""
    line = parse_line(value)
    return -line if line is not None else None
