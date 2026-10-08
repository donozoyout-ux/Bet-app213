import re
from .client import SourceError

BASE = 'https://football.goaloo.com'


async def discover_seasons(client, league_id: int, start_year=2024, overlap=False):
    payload = await client.get(f'{BASE}/jsData/leagueSeason/sea{league_id}.json')
    seasons = payload.get('SeasonList')
    if not isinstance(seasons, list) or not all(isinstance(s, str) and re.fullmatch(r'\d{4}(?:-\d{4})?', s) for s in seasons):
        raise SourceError('SeasonList schema changed')
    # Delayed cups can keep a 2023 label while their finals were played in 2024.
    # National workers filter actual kickoff dates, so checking the prior archive
    # prevents missing those real fixtures without storing pre-2024 matches.
    threshold = start_year - 1 if overlap else start_year
    return sorted(s for s in seasons if int(s[-4:] if overlap else s[:4]) >= threshold)


async def season_data(client, league_id: int, season: str, schedule_format='league'):
    if not re.fullmatch(r'\d{4}(?:-\d{4})?', season):
        raise ValueError('Invalid season')
    if schedule_format not in {'league', 'cup'}:
        raise ValueError('Unsupported schedule format')
    prefix = 'c' if schedule_format == 'cup' else 's'
    return await client.get(f'{BASE}/jsData/matchResult/json/{season}/{prefix}{league_id}_en.json')
