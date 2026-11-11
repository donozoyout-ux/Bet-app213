"""Selection rules for explicit 25+25 fixture insertion."""
import pytest
from scripts.import_missing_europe_fixtures import select_missing, run


def manifest(conflicts=0):
    return {"leagues": [
        {"league": 6, "conflict_total": conflicts, "seasons": [
            {"season": "2024-2025", "missing": [
                {"external_match_id": 12, "status": "finished"},
                {"external_match_id": 13, "status": "scheduled"},
                {"external_match_id": 14, "status": "finished"}]}]},
        {"league": 7, "conflict_total": 0, "seasons": [
            {"season": "2024-2025", "missing": [
                {"external_match_id": 99, "status": "finished"}]}]}]}


def test_only_missing_finished_are_selected():
    assert select_missing(manifest(), 6, 25) == [
        ("2024-2025", 12), ("2024-2025", 14)]
    assert select_missing(manifest(), 7, 25) == [("2024-2025", 99)]


def test_conflicted_source_aborts_before_write():
    with pytest.raises(RuntimeError, match="identity conflicts"):
        select_missing(manifest(conflicts=1), 6, 25)


@pytest.mark.parametrize("limit", [0, 26, -1])
def test_limit_is_bounded(limit):
    with pytest.raises(ValueError):
        select_missing(manifest(), 6, limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("ids,limit", [
    ([6, 6], 25), ([9], 25), ([6, 7, 9], 25), ([6], 26), ([], 1)
])
async def test_reject_invalid_import_before_any_database_or_source(ids, limit):
    with pytest.raises(ValueError):
        await run(ids, limit)
