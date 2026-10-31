from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest
from src.archive.retention_plan import active_window, classify_match, disk_scenario

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def test_istanbul_midnight_and_four_calendar_days():
    start, end = active_window(NOW)
    assert start.isoformat() == '2026-10-30T21:00:00+00:00'
    assert end.isoformat() == '2026-11-03T21:00:00+00:00'
    assert classify_match(start, 'finished', NOW) == 'retain_active'
    assert classify_match(end-timedelta(microseconds=1), 'scheduled', NOW) == 'retain_active'
    assert classify_match(end, 'scheduled', NOW) == 'retain_future'
    assert classify_match(start-timedelta(seconds=1), 'finished', NOW) == 'archive_candidate_pending_evidence'


@pytest.mark.parametrize('status', ['scheduled', 'postponed', 'abandoned', 'live', 'unknown'])
def test_unresolved_history_is_retained(status):
    assert classify_match(NOW-timedelta(days=10), status, NOW) == 'retain_unresolved'


def test_unknown_and_naive_dates():
    assert classify_match(None, 'finished', NOW) == 'retain_unknown_date'
    with pytest.raises(ValueError): active_window(datetime(2026, 10, 31))
    with pytest.raises(ValueError): classify_match(datetime(2020, 1, 1), 'finished', NOW)


def test_scenario_does_not_promise_disk_reclamation():
    result = disk_scenario(802936511, 345043022)
    assert result['after_this_plan_bytes'] == result['before_bytes']
    assert result['guaranteed_reclaimed_bytes'] == 0
    assert result['illustrative_after_rewrite_bytes'] == 457893489
    assert result['cleanup_authorized'] is False
    with pytest.raises(ValueError): disk_scenario(1, 2)


def test_measured_components_do_not_double_count_toast():
    data = json.loads(Path('docs/storage-observation-2026-10-31.json').read_text())
    for table in data['sizes']:
        assert table['total_bytes'] == sum(table[k] for k in ('heap_bytes', 'index_bytes', 'toast_bytes'))


def test_report_is_readonly_and_bounded():
    sql = Path('scripts/storage_readonly_report.sql').read_text()
    assert 'REPEATABLE READ READ ONLY' in sql
    assert "statement_timeout = '30s'" in sql
    assert sql.rstrip().endswith('ROLLBACK;')
    import re
    assert not re.search(r'\b(DELETE|TRUNCATE|VACUUM|INSERT|UPDATE|ALTER|DROP|CREATE)\b', sql, re.I)
