"""Read-only, portable historical archive pilot; never deletes or modifies source.

Exports finished matches for a specific Goaloo league/season to gzip JSONL.
Restores the archive into a fresh temporary SQLite database, checking SHA256,
row counts, uniqueness and archive parity; NOT a live PostgreSQL restore.
Does not upload anywhere or include users/coupons/credentials.
"""
import argparse
import asyncio
import gzip
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import aliased
from src.db import database
from src.models import (League, Season, Match, Team, Bookmaker, MatchStatistics,
                        Odds1X2, AsianHandicap, AsianTotals)

BOOKS = ("Crown", "Bet365", "Sbobet")
MARKETS = ((Odds1X2, "1x2"), (AsianHandicap, "ah"), (AsianTotals, "ou"))
FIELDS = {
    "1x2": ("home", "draw", "away"),
    "ah": ("line", "home", "away"),
    "ou": ("line", "over", "under"),
}
STAT_FIELDS = ("home_corners", "away_corners", "home_yellow_cards",
               "away_yellow_cards", "home_red_cards", "away_red_cards",
               "home_corners_ht", "away_corners_ht", "collection_status",
               "is_final", "source")


def normalized(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def encoded(row):
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=normalized, allow_nan=False)


async def collect(league_external_id, season_name):
    """Query a bounded season with no write statements; snapshots may change concurrently."""
    home, away = aliased(Team), aliased(Team)
    async with database.session() as session:
        matches = (await session.execute(
            select(Match, home.external_id, home.name, away.external_id, away.name)
            .join(League, Match.league_id == League.id)
            .join(Season, Match.season_id == Season.id)
            .join(home, Match.home_team_id == home.id)
            .join(away, Match.away_team_id == away.id)
            .where(League.external_id == league_external_id,
                   Season.season_name == season_name, Match.status == "finished",
                   Match.kickoff_at >= datetime(2024, 1, 1, tzinfo=timezone.utc))
            .order_by(Match.external_match_id))).all()
        result = []
        for match, home_id, home_name, away_id, away_name in matches:
            item = {"match_id": match.external_match_id, "league": league_external_id,
                    "season": season_name, "kickoff": normalized(match.kickoff_at),
                    "home_id": home_id, "away_id": away_id,
                    "home_name": home_name, "away_name": away_name,
                    "ft_home": match.ft_home, "ft_away": match.ft_away,
                    "ht_home": match.ht_home, "ht_away": match.ht_away,
                    "status": match.status, "odds": [], "stats": None}
            stats = await session.get(MatchStatistics, match.id)
            if stats is not None:
                item["stats"] = {key: getattr(stats, key) for key in STAT_FIELDS}
            for model, market in MARKETS:
                quote_rows = (await session.execute(
                    select(Bookmaker.name, model).join(Bookmaker, model.bookmaker_id == Bookmaker.id)
                    .where(model.match_id == match.id, Bookmaker.name.in_(BOOKS))
                    .order_by(Bookmaker.name))).all()
                for book, quote in quote_rows:
                    fields = {f"{stage}_{side}": getattr(quote, f"{stage}_{side}")
                              for stage in ("opening", "closing") for side in FIELDS[market]}
                    item["odds"].append({"bookmaker": book, "market": market, **fields})
            result.append(item)
        return result


def write_archive(rows, output):
    """Deterministic compressed payload plus a content-addressed manifest."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: row["match_id"])
    if len({row["match_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate source match IDs")
    with output.open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=9, mtime=0, fileobj=target) as zipped:
            for row in rows:
                zipped.write((encoded(row) + "\n").encode("utf-8"))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "file": output.name, "sha256": checksum,
                "match_count": len(rows), "odds_count": sum(len(r["odds"]) for r in rows),
                "league": rows[0]["league"] if rows else None,
                "season": rows[0]["season"] if rows else None,
                "format": "jsonl.gz", "contains_personal_account_data": False}
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def restore_verify(archive, manifest, db_path):
    """Isolated logical restore test. Refuses mismatched hashes and duplicates."""
    archive = Path(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Archive checksum mismatch")
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE fixtures (match_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("""CREATE TABLE quotes (
            match_id INTEGER NOT NULL, bookmaker TEXT NOT NULL, market TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(match_id,bookmaker,market),
            FOREIGN KEY(match_id) REFERENCES fixtures(match_id))""")
        conn.execute("PRAGMA foreign_keys=ON")
        seen = quote_count = 0
        with gzip.open(archive, "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["league"] != manifest["league"] or row["season"] != manifest["season"]:
                    raise ValueError("Archive partition mismatch")
                conn.execute("INSERT INTO fixtures VALUES (?,?)", (row["match_id"], encoded(row)))
                for quote in row["odds"]:
                    if quote["bookmaker"] not in BOOKS or quote["market"] not in FIELDS:
                        raise ValueError("Unknown quote bookmaker/market")
                    conn.execute("INSERT INTO quotes VALUES (?,?,?,?)",
                                 (row["match_id"], quote["bookmaker"], quote["market"], encoded(quote)))
                    quote_count += 1
                seen += 1
        conn.commit()
        if seen != manifest["match_count"] or quote_count != manifest["odds_count"]:
            raise ValueError("Archive count mismatch")
        restored = conn.execute("SELECT payload FROM fixtures ORDER BY match_id").fetchall()
        if len(restored) != seen:
            raise ValueError("Restore row count mismatch")
        for payload, in restored:
            parsed = json.loads(payload)
            if len(parsed["odds"]) != conn.execute(
                "SELECT COUNT(*) FROM quotes WHERE match_id=?", (parsed["match_id"],)).fetchone()[0]:
                raise ValueError("Restored quote parity mismatch")
        return {"verified_matches": seen, "verified_quotes": quote_count, "sha256": manifest["sha256"]}
    finally:
        conn.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int, required=True, choices=(6, 7))
    parser.add_argument("--season", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not database.configured:
        raise RuntimeError("Existing DATABASE_URL required")
    rows = await collect(args.league, args.season)
    if not rows:
        raise RuntimeError("No eligible finished source fixtures")
    manifest = write_archive(rows, args.output)
    with tempfile.TemporaryDirectory() as temp:
        proof = restore_verify(args.output, manifest, Path(temp) / "restore.sqlite3")
    print(json.dumps({"manifest": manifest, "isolated_restore": proof}, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(database.close())
