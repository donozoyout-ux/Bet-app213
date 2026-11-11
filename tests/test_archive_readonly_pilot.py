"""Local archive integrity checks with no production access."""
import json
import sqlite3
import pytest
from scripts.archive_readonly_pilot import write_archive, restore_verify


def sample():
    return [{"match_id": 202411, "league": 6, "season": "2024-2025",
             "kickoff": "2024-08-04T10:00:00+00:00", "status": "finished",
             "home_id": 11, "away_id": 12, "home_name": "A", "away_name": "B",
             "ft_home": 1, "ft_away": 0, "ht_home": 0, "ht_away": 0,
             "stats": {"home_corners": None}, "odds": [
                 {"bookmaker": "Crown", "market": "1x2", "opening_home": 1.7,
                  "closing_home": 1.8}]}]


def test_isolated_restore_proves_parity(tmp_path):
    archive = tmp_path / "season.jsonl.gz"
    manifest = write_archive(sample(), archive)
    restored = restore_verify(archive, manifest, tmp_path / "separate.sqlite3")
    assert restored["verified_matches"] == 1
    assert restored["verified_quotes"] == 1
    assert manifest["match_count"] == 1


def test_deterministic_archive_bytes(tmp_path):
    first, second = tmp_path / "a.gz", tmp_path / "b.gz"
    one, two = write_archive(sample(), first), write_archive(sample(), second)
    assert one["sha256"] == two["sha256"]


def test_corruption_is_rejected_before_restore(tmp_path):
    archive = tmp_path / "one.gz"
    manifest = write_archive(sample(), archive)
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="checksum"):
        restore_verify(archive, manifest, tmp_path / "restore.sqlite")


def test_duplicate_matches_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        write_archive(sample() * 2, tmp_path / "duplicate.gz")


def test_manifest_count_mismatch_fails(tmp_path):
    archive = tmp_path / "one.gz"
    manifest = write_archive(sample(), archive)
    manifest["odds_count"] += 1
    with pytest.raises(ValueError, match="count mismatch"):
        restore_verify(archive, manifest, tmp_path / "restore.sqlite")


def test_partition_mismatch_fails(tmp_path):
    archive = tmp_path / "one.gz"
    manifest = write_archive(sample(), archive)
    manifest["season"] = "2025-2026"
    with pytest.raises(ValueError, match="partition mismatch"):
        restore_verify(archive, manifest, tmp_path / "restore.sqlite")
