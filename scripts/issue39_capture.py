"""Read-only public API / Goaloo evidence capture; never enqueue source jobs."""
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from src.scrapers.goaloo.client import GoalooClient
from src.scrapers.goaloo.seasons import discover_seasons, season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches


async def main(directory):
    directory.mkdir(parents=True, exist_ok=True)
    summary = {'captured_at': datetime.now(timezone.utc).isoformat(), 'leagues': []}
    async with httpx.AsyncClient(base_url='https://bet-app213.onrender.com', timeout=60) as api, GoalooClient() as source:
        for endpoint in ['status', 'odds-coverage', 'diagnostics']:
            response = await api.get('/api/' + endpoint)
            response.raise_for_status()
            (directory / (endpoint + '.json')).write_text(response.text, encoding='utf-8')
        leagues = (await api.get('/api/leagues')).json()
        for league in leagues:
            stored = {}
            offset = 0
            while True:
                response = await api.get('/api/matches', params={'league': league['id'], 'limit': 200, 'offset': offset})
                response.raise_for_status()
                body = response.json()
                rows = body['items'] if isinstance(body, dict) else body
                if not rows:
                    break
                stored.update({row['external_match_id']: row for row in rows})
                offset += len(rows)
            (directory / f"db-{league['external_id']}.json").write_text(json.dumps(list(stored.values())), encoding='utf-8')
            found = {}
            errors = []
            for season in await discover_seasons(source, league['external_id'], 2024):
                payload = await season_data(source, league['external_id'], season)
                (directory / f"source-{league['external_id']}-{season}.json").write_text(json.dumps(payload), encoding='utf-8')
                for round_number in discover_rounds(payload):
                    for row in parse_matches(payload, round_number, errors):
                        found[row['external_match_id']] = row
            summary['leagues'].append({'external_id': league['external_id'], 'db_unique': len(stored), 'source_unique': len(found), 'missing_ids': sorted(found.keys() - stored.keys()), 'db_only_ids': sorted(stored.keys() - found.keys()), 'parse_failed': errors})
            print(league['external_id'], len(stored), len(found), len(errors), flush=True)
            (directory / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1])))
