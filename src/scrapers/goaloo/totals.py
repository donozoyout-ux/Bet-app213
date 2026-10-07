from .handicap import parse_line


def total_line(value):
    line = parse_line(value)
    if line is not None and line < 0:
        raise ValueError('Asian total cannot be negative')
    return line
