"""Manually supervised insert-only 2024+ Polish/Danish finished fixtures.

Run only after source/DB manifest review. Never runs from app startup.
"""
import argparse
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import fields
import json

from sqlalchemy import select, text

from scripts.audit_missing_europe_fixtures import audit, source_index, mismatch, CUTOFF
from scripts.europe_bounded_history import REPORT, selected_records, check_api_health
from scripts.verified_odds_backfill import capacity, batch_health
from src.db import database
from src.models import League, Match, ScraperJob, JobItem, utcnow
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.competitions import Competition, validate_identity
from src.scrapers.goaloo.seasons import season_data
from src.scrapers.goaloo.odds import fetch_odds
from src.scrapers.goaloo.statistics import fetch_statistics
from src.jobs.storage import store_match, store_statistics
from src.jobs.verified_odds import fill_only, is_complete


def select_missing(manifest, league_id, limit):
    if not 1 <= limit <= 25:
        raise ValueError("No more than 25 missing fixtures per league")
    entry = next(row for row in manifest["leagues"] if row["league"] == league_id)
    if entry["conflict_total"]:
        raise RuntimeError("Source identity conflicts: review before inserting")
    rows = []
    for season in entry["seasons"]:
        for fixture in season["missing"]:
            if fixture["status"] == "finished":
                rows.append((season["season"], fixture["external_match_id"]))
    return rows[:limit]


async def run(ids, limit=25):
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= {6, 7} or not 1 <= limit <= 25:
        raise ValueError("Only Poland 6 / Denmark 7, max 25 per league")
    if not database.configured:
        raise RuntimeError("Existing DATABASE_URL required")
    await batch_health()
    manifest = await audit(ids)  # Fresh DB/source comparison, no stale hardcoded 678.
    records = {r["external_id"]: r for r in selected_records(ids, json.loads(REPORT.read_text(encoding="utf-8")))}
    results = []
    async with database.engine.connect() as lock:
        if not await lock.scalar(text("SELECT pg_try_advisory_lock(213090)")):
            raise RuntimeError("Another supervised history operation is running")
        await lock.commit()
        try:
            async with GoalooClient() as client:
                for league_id in ids:
                    await batch_health()
                    async with database.session() as session:
                        before = await capacity(session)
                        league = await session.scalar(select(League).where(League.external_id == league_id))
                        if league is None or not league.enabled:
                            raise RuntimeError("Only already approved live leagues")
                        job = (await session.scalars(select(ScraperJob).where(
                            ScraperJob.league_id == league.id,
                            ScraperJob.kind == "europe_bounded"))).one()
                        if job.status != "manual_paused":
                            raise RuntimeError("Existing supervised checkpoint must be parked")
                        league_pk, job_id = league.id, job.id
                    identity = Competition(**{field.name: records[league_id][field.name]
                                              for field in fields(Competition) if field.name in records[league_id]})
                    selected = select_missing(manifest, league_id, limit)
                    indexed = {}
                    added = skipped = source_errors = 0
                    for season, external_id in selected:
                        await check_api_health()
                        async with database.session() as session:
                            await capacity(session)
                        if season not in indexed:
                            payload = await season_data(client, league_id, season)
                            validate_identity(identity, payload, season)
                            indexed[season] = source_index(payload, league_id)
                        row = indexed[season].get(external_id)
                        if (row is None or row["status"] != "finished" or
                                row.get("kickoff_at") is None or row["kickoff_at"] < CUTOFF or
                                row["kickoff_at"] > utcnow() - timedelta(hours=3)):
                            raise SourceError("Source fixture changed since audit; abort batch")
                        # Do not hold a database transaction while requesting Goaloo.
                        odds = stats = None
                        issues = []
                        try:
                            odds = await fetch_odds(client, external_id, True)
                        except SourceError:
                            issues.append("odds_source_error")
                        try:
                            stats = await fetch_statistics(client, external_id, league_id)
                        except SourceError:
                            issues.append("statistics_source_error")
                        async with database.session() as session:
                            await capacity(session)
                            await session.execute(text("SELECT pg_advisory_xact_lock(213003)"))
                            existing = await session.scalar(select(Match).where(
                                Match.external_match_id == external_id))
                            if existing is not None:
                                skipped += 1
                                await session.rollback()
                                continue  # Never overwrite a concurrent live insert.
                            match = await store_match(session, league_pk, season, row)
                            await session.flush()
                            if odds:
                                await fill_only(session, match.id, external_id, job_id, odds)
                                match.odds_complete = await is_complete(session, match.id)
                            if stats:
                                await store_statistics(session, match, stats)
                            item = JobItem(job_id=job_id, match_id=match.id,
                                           status="completed" if not issues else "failed",
                                           error=";".join(issues) or None)
                            session.add(item)
                            job = await session.get(ScraperJob, job_id)
                            job.current_season = season
                            job.current_round = row["round"]
                            job.processed_matches += 1
                            job.failed_matches += int(bool(issues))
                            job.checkpoint_at = utcnow()
                            await session.commit()
                        added += 1
                        source_errors += len(issues)
                        if source_errors >= 3:
                            break
                    async with database.session() as session:
                        after = await capacity(session)
                    results.append(dict(league=league_id, missing_at_audit=len(selected),
                                        added=added, skipped_concurrent=skipped,
                                        source_errors=source_errors,
                                        before=before, after=after))
                    await batch_health()
                    if source_errors >= 3:
                        break
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(213090)"))
            await lock.commit()
    print(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", type=int, action="append", required=True)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    async def main():
        try:
            await run(args.league, args.limit)
        finally:
            await database.close()

    asyncio.run(main())
