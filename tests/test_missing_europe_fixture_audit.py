"""No-network guardrail tests for the read-only Europe fixture audit."""
from datetime import datetime, timezone, timedelta
import pytest

from scripts.audit_missing_europe_fixtures import CUTOFF, mismatch, audit


def fixture():
    kickoff = datetime(2025, 3, 2, 12, tzinfo=timezone.utc)
    source = dict(external_match_id=123, external_league_id=6,
                  home_external_id=11, away_external_id=22,
                  kickoff_at=kickoff, ft_home=2, ft_away=1,
                  ht_home=1, ht_away=0)
    stored = {**source, "league_external_id": 6}
    return stored, source


def test_audit_identity_matches_known_source():
    stored, source = fixture()
    assert mismatch(stored, source, 6) is None


@pytest.mark.parametrize("field,value", [
    ("home_external_id", 999), ("away_external_id", 999),
    ("kickoff_at", datetime(2025, 3, 3, tzinfo=timezone.utc)),
    ("ft_home", 7), ("ht_away", 4),
])
def test_audit_rejects_identity_and_score_conflicts(field, value):
    stored, source = fixture()
    stored[field] = value
    assert mismatch(stored, source, 6) == field + "_conflict"


def test_audit_detects_cross_league_collision():
    stored, source = fixture()
    stored["league_external_id"] = 7
    assert mismatch(stored, source, 6) == "league_conflict"


def test_audit_does_not_flag_unknown_scores():
    stored, source = fixture()
    stored["ht_home"] = None
    source["ht_home"] = 1
    assert mismatch(stored, source, 6) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[], [5], [6, 6], [6, 7, 29]])
async def test_audit_rejects_unapproved_scope_before_database(ids):
    with pytest.raises(ValueError):
        await audit(ids)


def test_audit_cutoff_is_explicit_and_utc():
    assert CUTOFF == datetime(2024, 1, 1, tzinfo=timezone.utc)
