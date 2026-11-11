"""Explicit, resumable import: 1-3 reviewed leagues, <=25 matches per league/run.

Uses the existing PostgreSQL only. Runs outside the HTTP process. Import-only
leagues stay disabled until history and live quotes are independently reviewed.
"""
import argparse
import asyncio
from dataclasses import fields
from datetime import datetime, timezone, timedelta
import json
import os
import httpx
from pathlib import Path
from sqlalchemy import select, text
from src.db import database
from src.models import League, Match, ScraperJob, JobItem, utcnow
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.competitions import Competition, validate_identity, upsert_competitions
from src.scrapers.goaloo.seasons import season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from src.scrapers.goaloo.odds import fetch_odds
from src.scrapers.goaloo.statistics import fetch_statistics
from src.jobs.storage import store_match, store_odds, store_statistics
from src.jobs.europe_capacity import check_capacity

REPORT = Path(__file__).resolve().parents[1] / 'docs/europe-quality-live.json'


async def check_api_health():
    """A supervised workflow stops between commits if the live API is unhealthy."""
    url = os.getenv('EUROPE_API_GUARD_URL')
    if not url:
        return
    if url != 'https://bet-app213.onrender.com/health':
        raise ValueError('Unexpected API health guard URL')
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError('Historical import paused: live API health guard failed') from exc


def selected_records(ids, report):
    if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids):
        raise ValueError('Select 1-3 distinct leagues')
    records = {r['external_id']: r for r in report['competitions']}
    result = []
    for id in ids:
        row = records.get(id)
        if not row or not row.get('approved') or not row.get('verified') or row.get('error'):
            raise ValueError(f'League {id} has not passed source review')
        result.append(row)
    return result


async def run(ids, limit, albania_pilot=False, recent_first=False, activate=False):
    if not 1 <= limit <= 25:
        raise ValueError('Per-league limit must be 1-25')
    if activate and (albania_pilot or not recent_first or not set(ids)<={6,7}):
        raise ValueError('Activation requires recent-first reviewed Poland/Denmark batch')
    if albania_pilot:
        records = pilot_records(ids, limit)
    else:
        records = selected_records(ids, json.loads(REPORT.read_text(encoding='utf-8')))
    if not database.configured:
        raise RuntimeError('Existing DATABASE_URL is required')
    summary = []
    # Do not initialize schema or alter production scope from a historical tool.
    async with database.engine.connect() as lock:
        # Independent import lock: does not hold the live worker's lock.
        if not await lock.scalar(text('SELECT pg_try_advisory_lock(213090)')):
            raise RuntimeError('A supervised Europe import is already running')
        await lock.commit()
        try:
            async with GoalooClient() as client:
                for record in records:
                    await check_api_health()
                    async with database.session() as session:
                        before = await check_capacity(session)
                        league = await session.scalar(select(League).where(League.external_id == record['external_id']))
                        if league is None:
                            league = (await upsert_competitions(session, [record]))[0]
                            league.enabled = False
                        job = await session.scalar(select(ScraperJob).where(
                            ScraperJob.league_id == league.id, ScraperJob.kind == 'europe_bounded'))
                        if job is None:
                            job = ScraperJob(league_id=league.id, kind='europe_bounded',
                                start_year=2024, status='manual_paused')
                            session.add(job)
                            await session.flush()
                        league_id, job_id = league.id, job.id
                        await session.commit()
                    imported = 0
                    failures = 0
                    counts = {}
                    identity = Competition(**{f.name: record[f.name] for f in fields(Competition) if f.name in record})
                    seasons = sorted(record['seasons'],reverse=True) if recent_first else record['seasons']
                    for season in seasons:
                        if imported >= limit:
                            break
                        payload = await season_data(client, record['external_id'], season)
                        validate_identity(identity, payload, season)
                        candidates = {}
                        for section in discover_rounds(payload):
                            errors = []
                            for row in parse_matches(payload, section, errors):
                                if row['external_league_id'] != record['external_id']:
                                    raise SourceError('Unexpected league in historical schedule')
                                if (row['status'] == 'finished' and row['kickoff_at']
                                        and datetime(2024,1,1,tzinfo=timezone.utc) <= row['kickoff_at'] <= utcnow()-(timedelta(hours=3) if recent_first else timedelta())):
                                    candidates[row['external_match_id']] = row
                            if errors:
                                raise SourceError('Unparsed historical schedule rows')
                        for row in sorted(candidates.values(), key=lambda r: (r['kickoff_at'], r['external_match_id']),reverse=recent_first):
                            if imported >= limit:
                                break
                            async with database.session() as session:
                                await check_capacity(session)
                                existing = await session.scalar(select(Match).where(Match.external_match_id == row['external_match_id']))
                                item = await session.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.match_id == existing.id)) if existing else None
                                if existing and (item is None or item.status in {'completed','source_unavailable'}):
                                    continue  # Never rewrite existing historical scores/settlements.
                                attempts = int(item.error.split(';',1)[0].split('=')[1]) if item and item.error and item.error.startswith('attempts=') else 0
                            # Network outside a database transaction; no API handler involved.
                            if imported % 5 == 0:
                                await check_api_health()
                            odds, stats, errors = None, None, []
                            try:
                                odds = await fetch_odds(client, row['external_match_id'], True)
                            except SourceError:
                                errors.append('odds_source_error')
                            try:
                                stats = await fetch_statistics(client, row['external_match_id'], record['external_id'])
                            except SourceError:
                                errors.append('statistics_source_error')
                            async with database.session() as session:
                                await check_capacity(session)
                                # Shared storage get-or-create is protected from concurrent writers.
                                await session.execute(text('SELECT pg_advisory_xact_lock(213003)'))
                                session.info['historical_import'] = True
                                await check_capacity(session)  # Refresh after waiting for the queue lock.
                                match = await session.scalar(select(Match).where(Match.external_match_id == row['external_match_id']))
                                if match is None:
                                    match = await store_match(session, league_id, season, row)
                                elif existing is None:
                                    continue  # A live writer inserted it while source requests ran.
                                if odds is not None:
                                    await store_odds(session, match, odds, True)
                                if stats is not None:
                                    await store_statistics(session, match, stats)
                                item = await session.scalar(select(JobItem).where(JobItem.job_id == job_id, JobItem.match_id == match.id))
                                if item is None:
                                    item = JobItem(job_id=job_id, match_id=match.id)
                                    session.add(item)
                                item.status = ('source_unavailable' if attempts >= 2 else 'failed') if errors else 'completed'
                                item.error = f'attempts={attempts+1};'+','.join(errors) if errors else None
                                job = await session.get(ScraperJob, job_id)
                                job.current_season, job.current_round = season, row['round']
                                job.processed_matches += 1
                                job.failed_matches += bool(errors)
                                await session.commit()
                            imported += 1
                            failures += bool(errors)
                            for book, markets in (odds or {}).items():
                                for market, values in markets.items():
                                    for key, value in values.items():
                                        if key.startswith(('opening_', 'closing_')) and value is not None:
                                            name = f'{book}/{market}/{key}'
                                            counts[name] = counts.get(name, 0) + 1
                            print(json.dumps({'league':record['external_id'],'match':row['external_match_id'],'errors':errors}),flush=True)
                    async with database.session() as session:
                        after = await check_capacity(session)
                    if activate:
                        await check_api_health()
                        from src.jobs.europe_admission import activate_reviewed
                        async with database.session() as session:
                            await check_capacity(session)
                            admission = await activate_reviewed(session,[record['external_id']])
                            await session.commit()
                        print(json.dumps({'activation':admission}),flush=True)
                    summary.append({'league':record['external_id'],'processed':imported,
                        'source_failures':failures,'populated_fields':counts,'before':before,'after':after})
        finally:
            await lock.execute(text('SELECT pg_advisory_unlock(213090)'))
            await lock.commit()
    print(json.dumps(summary, indent=2))


def pilot_records(ids, limit):
    # These pilots passed identity/schedule review, not the three-book quality
    # gate. Permit only a five-match observation, never broad import/activation.
    from src.scrapers.goaloo.competitions import approved_europe_snapshot
    if not 1 <= limit <= 5 or not ids or len(ids) != len(set(ids)) or not set(ids) <= {315,348}:
        raise ValueError('Albania observation permits only 315/348 and at most five matches each')
    rows = {r['external_id']:r for r in approved_europe_snapshot()}
    return [rows[id] for id in ids]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', type=int, action='append', required=True)
    parser.add_argument('--limit', type=int, default=25)
    args = parser.parse_args()
    async def main():
        try:
            await run(args.league, args.limit)
        finally:
            await database.close()
    asyncio.run(main())
