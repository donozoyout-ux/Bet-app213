"""Discover target competitions from Goaloo's catalog, then validate live data.

python -m src.scrapers.goaloo.competitions [--output report.json] [--seed]
No numeric competition IDs are defined in the target policy.
"""
import argparse
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import unicodedata
from .client import GoalooClient, SourceError
from .seasons import BASE, discover_seasons, season_data
from .rounds import discover_rounds
from .matches import parse_matches
from .odds import fetch_odds

CATALOG_URL = BASE + '/jsData/infoHeaderEn.js'
VERIFIED_PATH = Path(__file__).with_name('verified_competitions.json')

# Abbreviations and countries are catalog names, not remembered numeric IDs.
CLUB_TARGETS = [
    ('England','ENG PR'), ('Spain','SPA D1'), ('Italy','ITA D1'),
    ('Germany','GER D1'), ('France','FRA D1'), ('Turkey','TUR D1'),
    ('Netherlands','HOL D1'), ('Portugal','POR D1'), ('Belgium','BEL D1'),
    ('Scotland','SCO PR'), ('Austria','AUT D1'), ('Switzerland','SUI SL'),
    ('Greece','GRE D1'), ('Saudi Arabia','SPL'), ('United States','MLS'),
]
NATIONAL_TARGETS = [
    ('International','World Cup'),
    ('International','WCPEU'), ('International','FIFA WCQL'),
    ('International','WCPSA'), ('International','WCPAF'),
    ('International','WCPCA'), ('International','WCPO'), ('International','WCP-PO'),
    ('Europe','EURO Cup'), ('Europe','UEFA NL'), ('Americas','AMEC'),
    ('Africa','CAF NC'), ('Asia','AFC'), ('Americas','CGC'), ('International','INT FRL'),
]


def normalize_name(name):
    value = unicodedata.normalize('NFKC', html.unescape(name)).casefold()
    return ' '.join(value.split())


@dataclass
class Competition:
    external_id: int
    catalog_name: str
    country: str
    competition_type: str
    schedule_format: str
    priority: int
    name: str = ''
    latest_season: str | None = None
    seasons: list[str] | None = None
    verified: bool = False
    odds_endpoint: str = 'not_checked'
    odds_books: list[str] | None = None
    historical_matches: int = 0
    season_counts: dict | None = None
    verified_at: str | None = None
    error: str | None = None


def parse_catalog(source):
    """Parse JSON array literals only; never execute the provider's JavaScript."""
    entries = []
    for literal in re.findall(r'arr\[\d+\]\s*=\s*(\[.*?\]);', source, re.DOTALL):
        try:
            group = json.loads(literal)
            country = group[1]
            for record in group[4]:
                fields = record.split(',')
                external_id = int(fields[0])
                if external_id <= 0 or fields[2] not in {'1', '2'}:
                    raise ValueError('Invalid catalog ID/type')
                entries.append({'external_id': external_id, 'catalog_name': fields[1],
                                'country': country, 'schedule_format': 'cup' if fields[2] == '2' else 'league'})
        except (ValueError, TypeError, IndexError) as exc:
            raise SourceError('Competition catalog schema changed') from exc
    if not entries:
        raise SourceError('Competition catalog is empty')
    return entries


def select_targets(entries):
    targets = []
    for index, (country, name) in enumerate(CLUB_TARGETS + NATIONAL_TARGETS, 1):
        found = [item for item in entries if normalize_name(item['country']) == normalize_name(country)
                 and normalize_name(item['catalog_name']) == normalize_name(name)]
        if len(found) != 1:
            raise SourceError(f'Catalog target missing or ambiguous: {country} / {name}')
        targets.append(Competition(**found[0], competition_type='club' if index <= len(CLUB_TARGETS) else 'national', priority=index))
    if len({item.external_id for item in targets}) != len(targets):
        raise SourceError('Different target competitions share a catalog ID')
    return targets


def validate_identity(competition, payload, season):
    info = payload.get('LeagueInfo', [])
    try:
        short = info[2] if competition.schedule_format == 'cup' else info[5] if 'SubLeagueInfo' in payload else info[7]
        actual_season = info[3] if competition.schedule_format == 'cup' else info[2]
        if int(info[0]) != competition.external_id:
            raise SourceError('Competition ID or season does not match request')
        # Some cup files retain CURRENT-season LeagueInfo in archived schedules.
        # Their rows must instead substantiate the requested tournament interval.
        if competition.schedule_format != 'cup' and str(actual_season) != season:
            raise SourceError('Season does not match request')
        if normalize_name(short) != normalize_name(competition.catalog_name) or not isinstance(info[1], str) or not info[1].strip():
            raise SourceError('Competition name does not match catalog')
        return html.unescape(info[1]).strip()
    except SourceError:
        raise
    except (IndexError, TypeError, ValueError) as exc:
        raise SourceError('Competition identity schema changed') from exc


async def verify_competition(client, competition):
    competition.error = None
    try:
        seasons = await discover_seasons(client, competition.external_id, 2024, overlap=competition.competition_type == 'national')
        if not seasons:
            raise SourceError('No available tournaments overlapping 2024 onward')
        competition.seasons, competition.latest_season = seasons, seasons[-1]
        all_ids, sample = set(), None
        competition.season_counts = {}
        for season in seasons:
            payload = await season_data(client, competition.external_id, season, competition.schedule_format)
            competition.name = validate_identity(competition, payload, season)
            ids = set()
            for section in discover_rounds(payload):
                errors = []
                for match in parse_matches(payload, section, errors):
                    if match['external_league_id'] != competition.external_id:
                        raise SourceError('Schedule contains a different competition ID')
                    if match['kickoff_at'] is None or match['kickoff_at'].year < 2024:
                        continue
                    ids.add(match['external_match_id'])
                    if sample is None or match['status'] == 'finished' and sample['status'] != 'finished':
                        sample = match
            competition.season_counts[season] = len(ids)
            if ids and competition.schedule_format == 'cup':
                dated = [match for section in discover_rounds(payload) for match in parse_matches(payload, section, []) if match['kickoff_at'] is not None]
                if not any(int(season[:4]) <= match['kickoff_at'].year <= int(season[-4:]) + 1 for match in dated):
                    raise SourceError('Cup schedule dates do not substantiate requested season')
            all_ids.update(ids)
        if sample is None:
            raise SourceError('No valid 2024+ fixtures in available schedules')
        competition.historical_matches = len(all_ids)
        competition.verified = True
        competition.verified_at = datetime.now(timezone.utc).isoformat()
        try:
            odds = await fetch_odds(client, sample['external_match_id'], sample['status'] == 'finished')
            competition.odds_books = [book for book, markets in odds.items() if any(
                value is not None for market in markets.values() for key, value in market.items()
                if key.startswith(('opening_', 'latest_', 'closing_')))]
            competition.odds_endpoint = 'ok' if competition.odds_books else 'no_prices'
        except SourceError:
            # Odds availability is independent of the verified competition/schedule.
            competition.odds_endpoint = 'unavailable'
    except (SourceError, ValueError) as exc:
        competition.verified = False
        competition.error = str(exc)
    return competition


async def discover_competitions(client=None):
    if client is None:
        async with GoalooClient() as source:
            return await discover_competitions(source)
    targets = select_targets(parse_catalog(await client.get_text(CATALOG_URL)))
    # Sequential verification uses the existing rate limiter; never bulk-fetch odds.
    for target in targets:
        await verify_competition(client, target)
        print(f'{target.name or target.catalog_name}: id={target.external_id} type={target.competition_type} season={target.latest_season} verified={target.verified} odds={target.odds_endpoint}', flush=True)
    return targets


def verified_snapshot():
    """Last live-verified catalog snapshot; IDs are generated by the discovery CLI."""
    if not VERIFIED_PATH.exists():
        return []
    report = json.loads(VERIFIED_PATH.read_text(encoding='utf-8'))
    return [item for item in report['competitions'] if item['verified']]


async def upsert_competitions(session, records):
    from sqlalchemy import select
    from src.models import League, utcnow
    result = []
    for item in records:
        record = asdict(item) if isinstance(item, Competition) else item
        if not record.get('verified') or not record.get('verified_at'):
            raise ValueError('Only live-verified competitions may be seeded')
        if not isinstance(record.get('external_id'), int) or record['external_id'] <= 0 or record.get('competition_type') not in {'club','national'} or record.get('schedule_format') not in {'league','cup'}:
            raise ValueError('Invalid verified competition identity or type')
        row = await session.scalar(select(League).where(League.external_id == record['external_id']))
        if row is None:
            row = League(external_id=record['external_id'], enabled=True)
            session.add(row)
        for field in ('name','country','competition_type','priority','schedule_format','latest_season'):
            setattr(row, field, record[field])
        row.verified_at = datetime.fromisoformat(record['verified_at'])
        row.source, row.backfill_from_year, row.updated_at = 'goaloo', 2024, utcnow()
        result.append(row)
    await session.flush()
    return result


async def main_async(args):
    competitions = await discover_competitions()
    report = {'catalog_url': CATALOG_URL, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'competitions': [asdict(item) for item in competitions],
              'notes': ['EURO finals and qualifiers share the Goaloo EURO Cup competition; stage/group labels distinguish them.',
                        'World Cup qualifiers are separate confederation competitions, not a guessed aggregate ID.',
                        'Match counts are unique dated 2024+ fixtures currently published, not odds completeness guarantees.']}
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.seed:
        from src.db import database
        try:
            if not database.configured:
                raise ValueError('DATABASE_URL must be configured to seed')
            await database.initialize()
            async with database.session() as session:
                from sqlalchemy import text
                if session.bind.dialect.name == 'postgresql':
                    await session.execute(text('SELECT pg_advisory_xact_lock(213001)'))
                await upsert_competitions(session, [item for item in competitions if item.verified])
                await session.commit()
        finally:
            await database.close()
    return 0 if all(item.verified for item in competitions) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Save the verification report as UTF-8 JSON')
    parser.add_argument('--seed', action='store_true', help='Upsert only the competitions verified by this run')
    try:
        code = asyncio.run(main_async(parser.parse_args()))
    except Exception as exc:
        # Database/authentication errors must never echo connection strings.
        print(f'Verification failed: {type(exc).__name__}', flush=True)
        code = 1
    raise SystemExit(code)
