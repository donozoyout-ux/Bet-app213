import re
from .client import SourceError

BASE = 'https://football.goaloo.com'


async def discover_seasons(client, league_id: int, start_year=2024):
    payload = await client.get(f'{BASE}/jsData/leagueSeason/sea{league_id}.json')
    seasons = payload.get('SeasonList')
    if not isinstance(seasons, list) or not all(isinstance(s, str) and re.fullmatch(r'\d{4}-\d{4}', s) for s in seasons):
        raise SourceError('SeasonList schema changed')
    return sorted(s for s in seasons if int(s[:4]) >= start_year)


async def season_data(client, league_id: int, season: str):
    if not re.fullmatch(r'\d{4}-\d{4}', season):
        raise ValueError('Invalid season')
    return await client.get(f'{BASE}/jsData/matchResult/json/{season}/s{league_id}_en.json')
