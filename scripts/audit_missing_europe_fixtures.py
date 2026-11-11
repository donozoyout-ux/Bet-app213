"""Read-only Goaloo-to-PostgreSQL audit for missing Poland/Denmark 2024+ fixtures.

Never inserts fixtures, odds, jobs, or checkpoints. Intended to precede a
separate supervised import reviewed from the resulting manifest.
"""
import argparse
import asyncio
from dataclasses import fields
from datetime import datetime, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import aliased

from src.db import database
from src.models import League, Match, Team
from src.scrapers.goaloo.client import GoalooClient, SourceError
from src.scrapers.goaloo.competitions import Competition, validate_identity
from src.scrapers.goaloo.seasons import season_data
from src.scrapers.goaloo.rounds import discover_rounds
from src.scrapers.goaloo.matches import parse_matches
from scripts.europe_bounded_history import REPORT, selected_records

CUTOFF = datetime(2024, 1, 1, tzinfo=timezone.utc)
ALLOWED = {6, 7}


def source_index(payload, league_id):
    """Deduplicate identical round appearances, reject contradictory identities."""
    result = {}
    for section in discover_rounds(payload):
        errors = []
        for row in parse_matches(payload, section, errors):
            if row["external_league_id"] != league_id:
                raise SourceError("Unexpected league ID in source round")
            kickoff = row.get("kickoff_at")
            if kickoff is None:
                raise SourceError("Missing kickoff in source fixture; refusing partial audit")
            kickoff = utc_value(kickoff)
            if kickoff < CUTOFF:
                continue
            key = row["external_match_id"]
            original = result.get(key)
            if original and any(original.get(field) != row.get(field) for field in
                    ("home_external_id", "away_external_id", "kickoff_at")):
                raise SourceError("Contradictory source fixture identity")
            result[key] = row
        if errors:
            raise SourceError("Unparsed source schedule rows; refusing partial audit")
    return result


def utc_value(value):
    """PostgreSQL may return naive UTC timestamps despite aware source dates."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def mismatch(stored, source, league_id):
    if stored["league_external_id"] != league_id:
        return "league_conflict"
    for key in ("home_external_id", "away_external_id", "kickoff_at"):
        if (utc_value(stored[key]) if key == "kickoff_at" else stored[key]) != (utc_value(source[key]) if key == "kickoff_at" else source[key]):
            return key + "_conflict"
    for key in ("ft_home", "ft_away", "ht_home", "ht_away"):
        if stored[key] is not None and source.get(key) is not None and stored[key] != source[key]:
            return key + "_conflict"
    return None


async def audit(ids):
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= ALLOWED:
        raise ValueError("Read-only scope is Poland 6 / Denmark 7")
    if not database.configured:
        raise RuntimeError("Existing DATABASE_URL is required")
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    records = selected_records(ids, report)
    output = {"mode": "read_only", "cutoff": CUTOFF.isoformat(), "leagues": []}
    home, away = aliased(Team), aliased(Team)
    async with GoalooClient() as client:
        for record in records:
            league_id = record["external_id"]
            identity = Competition(**{f.name: record[f.name] for f in fields(Competition)
                                      if f.name in record})
            async with database.session() as session:
                league = await session.scalar(select(League).where(League.external_id == league_id))
                if league is None or not league.enabled:
                    raise RuntimeError("League not admitted; refusing audit")
                query = (select(Match.external_match_id, Match.kickoff_at,
                                Match.ft_home, Match.ft_away, Match.ht_home, Match.ht_away,
                                League.external_id.label("league_external_id"),
                                home.external_id.label("home_external_id"),
                                away.external_id.label("away_external_id"))
                         .join(League, Match.league_id == League.id)
                         .join(home, Match.home_team_id == home.id)
                         .join(away, Match.away_team_id == away.id)
                         .where(Match.kickoff_at >= CUTOFF))
                stored = {}
                for item in (await session.execute(query)).mappings():
                    key = item["external_match_id"]
                    if key in stored:
                        raise RuntimeError("Duplicate stored external match ID")
                    stored[key] = dict(item)
            league_result = {"league": league_id, "seasons": [], "missing_total": 0,
                             "conflict_total": 0}
            seen = set()
            for season in record["seasons"]:
                payload = await season_data(client, league_id, season)
                validate_identity(identity, payload, season)
                fixtures = source_index(payload, league_id)
                missing, conflicts = [], []
                for match_id, source in sorted(fixtures.items()):
                    if match_id in seen:
                        continue
                    seen.add(match_id)
                    current = stored.get(match_id)
                    if current is None:
                        missing.append({"external_match_id": match_id,
                                        "kickoff_at": source["kickoff_at"].isoformat(),
                                        "status": source["status"]})
                    else:
                        issue = mismatch(current, source, league_id)
                        if issue:
                            conflicts.append({"external_match_id": match_id, "reason": issue})
                league_result["seasons"].append({
                    "season": season, "source_2024plus": len(fixtures),
                    "newly_missing": len(missing), "missing": missing,
                    "conflicts": conflicts,
                })
                league_result["missing_total"] += len(missing)
                league_result["conflict_total"] += len(conflicts)
            output["leagues"].append(league_result)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", type=int, action="append", required=True)
    args = parser.parse_args()

    async def main():
        try:
            print(json.dumps(await audit(args.league), indent=2, ensure_ascii=False))
        finally:
            await database.close()

    asyncio.run(main())
