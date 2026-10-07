"""Read-only deployment verification: python -m src.jobs.verify [--goaloo]."""
import argparse
import asyncio
import json
from src.config import settings
from src.db import database
from src.diagnostics import database_summary
from src.scrapers.goaloo.client import GoalooClient
from src.scrapers.goaloo.seasons import discover_seasons, season_data
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.odds import fetch_odds


async def goaloo_smoke():
    """Three real requests: seasons, one schedule, one match's odds. No storage."""
    async with GoalooClient() as client:
        seasons = await discover_seasons(client, 36, 2024)
        if not seasons:
            raise ValueError('No Premier League seasons')
        payload = await season_data(client, 36, seasons[0])
        rounds = discover_rounds(payload)
        matches = list(parse_matches(payload, rounds[0])) if rounds else []
        if not matches:
            raise ValueError('No schedule matches')
        match = matches[0]
        odds = await fetch_odds(client, match['external_match_id'], match['status'] == 'finished')
        if not odds:
            raise ValueError('No bookmaker odds')
        return {'seasons': seasons, 'schedule_season': seasons[0], 'round': rounds[0],
                'match_id': match['external_match_id'], 'bookmakers': sorted(odds)}


async def verify(source=False):
    report = {'app_env': settings.app_env, 'database_configured': database.configured,
              'worker_enabled': settings.worker_enabled,
              'auto_backfill_enabled': settings.auto_backfill_on_empty,
              'scraper_token_configured': bool(settings.scraper_token), 'database_connected': False}
    ok = False
    try:
        if database.configured:
            try:
                summary = await asyncio.wait_for(database_summary(database, verify_schema=True), timeout=15)
                report.update(summary)
                report['database_connected'] = True
                ok = summary['schema_ok'] and summary.get('league_count', 0) > 0
            except Exception as exc:
                report['database_error_type'] = type(exc).__name__
        if source:
            try:
                report['goaloo'] = await goaloo_smoke()
            except Exception as exc:
                report['goaloo_error_type'] = type(exc).__name__
                ok = False
    finally:
        await database.close()
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--goaloo', action='store_true', help='Check three live Goaloo endpoints')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(verify(args.goaloo)))


if __name__ == '__main__':
    main()
