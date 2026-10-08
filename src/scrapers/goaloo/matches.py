from datetime import datetime, timezone, timedelta
from .client import SourceError
from .results import parse_score, match_status
from .schedule import schedule_sections

SOURCE_TIMEZONE = timezone(timedelta(hours=8))


def parse_matches(payload, round_number, errors=None):
    """Schedule timestamps are UTC+8, as verified by league.js timeFromE8()."""
    try:
        teams = {int(t[0]): t[1] for t in payload.get('TeamInfo', payload.get('TeamList', []))}
        section = schedule_sections(payload)[round_number]
        rows = section['rows']
    except (KeyError, TypeError, IndexError) as exc:
        raise SourceError('League match schema changed') from exc
    for row in rows:
        try:
            if int(row[0]) <= 0 or int(row[4]) <= 0 or int(row[5]) <= 0:
                raise ValueError('Unresolved fixture participants')
            ht = parse_score(row[7])
            ft = parse_score(row[6])
            kickoff = datetime.strptime(row[3], '%Y-%m-%d %H:%M').replace(tzinfo=SOURCE_TIMEZONE).astimezone(timezone.utc) if row[3] else None
            yield {'external_match_id': int(row[0]), 'external_league_id': int(row[1]),
                   'round': round_number, 'round_label': section['label'], 'stage_key': section['key'], 'kickoff_at': kickoff, 'status': match_status(row[2]),
                   'home_external_id': int(row[4]), 'home_name': teams[int(row[4])],
                   'away_external_id': int(row[5]), 'away_name': teams[int(row[5])],
                   'ht_home': ht[0], 'ht_away': ht[1], 'ft_home': ft[0], 'ft_away': ft[1],
                   'raw': {'schedule': row, 'stage_key': section['key'], 'source_timezone': 'UTC+08:00'}}
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            external_id = str(row[0]) if isinstance(row, list) and row else 'unknown'
            message = f'Invalid Goaloo match row match_id={external_id}'
            if errors is None:
                raise SourceError(message) from exc
            errors.append({'external_match_id': external_id, 'error': message, 'raw': {'schedule': row}})
