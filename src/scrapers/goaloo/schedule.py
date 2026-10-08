"""Normalize real league, sub-league and cup groups without manufacturing rounds."""
import re
from .client import SourceError


def schedule_sections(payload):
    schedule = payload.get('ScheduleList')
    if not isinstance(schedule, dict):
        raise SourceError('ScheduleList schema changed')
    cups = {str(row[0]): row[2] for row in payload.get('CupKindList', [])}
    subs = {str(row[0]): row[1] for row in payload.get('SubLeagueInfo', [])}
    result = {}

    def visit(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, [*path, key])
        elif isinstance(value, list):
            if not all(isinstance(row, list) for row in value):
                raise SourceError('Schedule rows must be arrays')
            key = '/'.join(path)
            leaf = path[-1]
            round_match = re.fullmatch(r'R_(\d+)', leaf)
            # For cup groups the numeric filter is a section index; stage_key and
            # round_label preserve the provider's real stage/group identities.
            number = int(round_match[1]) if round_match and len(path) == 1 else len(result) + 1
            labels = []
            for part in path:
                group = re.fullmatch(r'G(\d+)(.*)', part)
                knockout = re.fullmatch(r'(?:K|R)(\d+)', part)
                if group:
                    labels.append(cups.get(group[1], group[1]) + (f' • Group {group[2]}' if group[2] else ''))
                elif part.startswith('sub_'):
                    labels.append(subs.get(part[4:], part))
                elif round_match and part == leaf:
                    labels.append(f'Round {round_match[1]}')
                elif knockout:
                    labels.append(cups.get(knockout[1], part))
                else:
                    labels.append(part)
            result[number] = {'key': key, 'label': ' • '.join(labels), 'rows': value}
        else:
            raise SourceError('Unexpected schedule section')
    visit(schedule, [])
    if not result:
        raise SourceError('No schedule sections found')
    return result
