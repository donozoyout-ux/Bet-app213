"""Discover target competitions from Goaloo's catalog, then validate live data.

python -m src.scrapers.goaloo.competitions [--output report.json] [--seed]
Catalog discovery remains independent of the explicit eight-league production rollout.
"""
PRODUCTION_LEAGUE_IDS = (36,31,34,8,11,30,16,23,103,113,2187,315,348,5,29)
UEFA_COMPETITIONS = {103: 'UEFA Champions League', 113: 'UEFA Europa League', 2187: 'UEFA Conference League'}

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
    sample_match_ids: dict | None = None


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



# Source catalog region names, not fabricated Goaloo competition identifiers.
# European domestic leagues are discovered, then independently source-verified
# before they can be seeded. No new league is automatically enabled.
EUROPEAN_COUNTRIES = frozenset({
    'Albania','Andorra','Armenia','Austria','Azerbaijan','Belarus','Belgium',
    'Bosnia','Bosnia and Herzegovina','Bulgaria','Croatia','Cyprus','Czech Republic',
    'Czechia','Denmark','England','Estonia','Faroe','Faroe Islands','Finland','France',
    'Georgia','Germany','Gibraltar','Greece','Hungary','Iceland','Ireland',
    'Israel','Italy','Kazakhstan','Kosovo','Latvia','Liechtenstein','Lithuania',
    'Luxembourg','Malta','Moldova','Montenegro','Netherlands','Northern Ireland',
    'North Macedonia','Norway','Poland','Portugal','Romania','Russia',
    'San Marino','Scotland','Serbia','Slovakia','Slovenia','Spain','Sweden',
    'Switzerland','Turkey','Turkiye','Türkiye','Ukraine','Wales',
})
def select_european_leagues(entries):
    """Catalog discovery only: leagues, not cups; no invented IDs or activation."""
    countries={normalize_name(name) for name in EUROPEAN_COUNTRIES}
    found={}
    for row in entries:
        if row.get('schedule_format')!='league' or normalize_name(row.get('country','')) not in countries:
            continue
        competition_id=row.get('external_id')
        if not isinstance(competition_id,int) or competition_id <= 0:
            raise SourceError('Invalid European league catalog ID')
        if competition_id in found and found[competition_id]!=row:
            raise SourceError(f'Ambiguous European league ID: {competition_id}')
        found[competition_id]=row
    return [Competition(**row,competition_type='club',priority=500+index)
            for index,row in enumerate(sorted(found.values(),key=lambda row:(normalize_name(row['country']),normalize_name(row['catalog_name']),row['external_id'])),1)]

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


async def verify_competition(client, competition, include_overlapping=False):
    competition.error = None
    try:
        seasons = await discover_seasons(client, competition.external_id, 2024, overlap=include_overlapping or competition.competition_type == 'national')
        if competition.competition_type == 'club':
            seasons = [s for s in seasons if int(s[-4:]) >= 2024]
        if not seasons:
            raise SourceError('No available tournaments overlapping 2024 onward')
        competition.seasons, competition.latest_season = seasons, seasons[-1]
        all_ids, sample = set(), None
        competition.season_counts = {}
        competition.sample_match_ids = {}
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
                if errors:
                    raise SourceError('Schedule contains unparsed fixtures; verification is incomplete')
            competition.season_counts[season] = len(ids)
            if ids:competition.sample_match_ids[season]=min(ids)
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



async def verify_uefa_competitions(client):
    """Verify UEFA cup identities using source schedules if catalog is blocked."""
    try:
        catalog = parse_catalog(await client.get_text(CATALOG_URL))
    except SourceError:
        # Goaloo sometimes responds 406 on its header catalog from Render.
        # Never trust IDs alone: verify both the competition name and fixtures.
        catalog = None

    targets = []
    markers = {103: 'champions league', 113: 'europa league',
               2187: 'conference league'}
    for offset, (external_id, title) in enumerate(UEFA_COMPETITIONS.items(), 20):
        item = None
        if catalog is not None:
            items = [entry for entry in catalog if entry['external_id'] == external_id]
            if len(items) != 1:
                raise SourceError(f'UEFA competition ID absent or ambiguous: {external_id}')
            item = items[0]
        else:
            seasons = await discover_seasons(client, external_id, 2024)
            if not seasons:
                raise SourceError(f'No UEFA seasons for competition {external_id}')
            for fmt in ('cup', 'league'):
                try:
                    payload = await season_data(client, external_id, seasons[-1], fmt)
                except SourceError:
                    continue
                info = payload.get('LeagueInfo', [])
                if not isinstance(info, list) or len(info) < 4 or str(info[0]) != str(external_id):
                    continue
                full_name = normalize_name(str(info[1]))
                if markers[external_id] not in full_name:
                    raise SourceError(f'UEFA competition name mismatch: {external_id}')
                try:
                    short_name = info[2] if fmt == 'cup' else info[5] if 'SubLeagueInfo' in payload else info[7]
                except (IndexError, TypeError):
                    continue
                item = {'external_id': external_id, 'catalog_name': short_name,
                        'country': 'Europe', 'schedule_format': fmt}
                break
            if item is None:
                raise SourceError(f'UEFA schedule identity unavailable: {external_id}')
        competition = Competition(**item, competition_type='club', priority=offset)
        await verify_competition(client, competition)
        if not competition.verified:
            raise SourceError(f'UEFA schedule verification failed: {external_id}')
        competition.name = title
        targets.append(competition)
    return targets

def verified_snapshot():
    """Last live-verified catalog snapshot; IDs are generated by the discovery CLI."""
    if not VERIFIED_PATH.exists():
        return []
    report = json.loads(VERIFIED_PATH.read_text(encoding='utf-8'))
    return [item for item in report['competitions'] if item['verified']]



def approved_europe_snapshot():
    """Only two source-verified pilot leagues, never the unchecked catalog."""
    report_path = Path(__file__).resolve().parents[3] / 'docs' / 'europe-first-batch-live.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    approved = {315, 348}
    if report.get('from_year') != 2024 or report.get('production_activation') is not False:
        raise SourceError('European pilot verification report is not suitable for activation')
    rows = {row['external_id']: row for row in report.get('competitions', []) if row.get('external_id') in approved}
    if set(rows) != approved:
        raise SourceError('Approved European pilot identities are missing')
    expected = {315: ('Albania', 'ALB D1'), 348: ('Albania', 'ALB D2')}
    for id, row in rows.items():
        if not row.get('verified') or not row.get('verified_at') or row.get('error') or row.get('country') != expected[id][0] or row.get('catalog_name') != expected[id][1] or row.get('schedule_format') != 'league' or row.get('competition_type') != 'club':
            raise SourceError('European pilot source identity is not verified')
    return [rows[id] for id in sorted(approved)]

def reviewed_europe_snapshot():
    """Reviewed catalog rows; only existing, well-populated 5/29 become active.

    Other rows remain import-only under the unchanged production scope rules.
    """
    path = Path(__file__).resolve().parents[3] / 'docs/europe-quality-live.json'
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('from_year') != 2024 or report.get('production_activation') is not False:
        raise SourceError('Invalid European quality review')
    approved = []
    allowed = {3,5,6,7,22,26,27,29,32,37,133,137}
    for row in report['competitions']:
        if not row.get('approved'):
            continue
        if (row['external_id'] not in allowed or not row.get('verified')
                or not row.get('verified_at') or row.get('error')
                or row.get('sample_count',0) < 6
                or row.get('complete_sample_count',0)/row['sample_count'] < .8
                or row.get('schedule_parse_error_count') != 0
                or row.get('schedule_request_error_rate') != 0):
            raise SourceError('European quality review failed admission checks')
        approved.append(row)
    return approved


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
            from src.config import settings
            row = League(external_id=record['external_id'], enabled=settings.app_env!='production' or record['external_id'] in PRODUCTION_LEAGUE_IDS)
            session.add(row)
        from src.config import settings
        if settings.app_env=='production' and record['external_id'] not in PRODUCTION_LEAGUE_IDS:row.enabled=False
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





async def apply_production_scope(session):
    """Explicit eight-league rollout, preserving every stored match and odds row."""
    from sqlalchemy import update
    from src.models import League
    await session.execute(update(League).where(League.external_id.not_in(PRODUCTION_LEAGUE_IDS)).values(enabled=False))
    await session.execute(update(League).where(League.external_id.in_(PRODUCTION_LEAGUE_IDS)).values(enabled=True))
