from .matches import parse_matches
from .rounds import discover_rounds
from .seasons import season_data
from .client import SourceError


async def fetch_match_details(client, external_id, league_id, season, schedule_format='league'):
    # The league JSON supplies authoritative scores for historical AND current matches.
    payload = await season_data(client, league_id, season, schedule_format)
    for round_number in discover_rounds(payload):
        for match in parse_matches(payload, round_number, []):
            if match['external_match_id'] == external_id:
                return match
    raise SourceError(f'Match absent from league schedule match_id={external_id}')
