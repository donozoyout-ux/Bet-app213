"""Guardrails for manual PostgreSQL backups; no network or real secrets."""
import pytest
from scripts.backup_render_guard import check_backup_inputs, EXPECTED_HOST

GOOD = f"postgresql://readonly:fake-credential@{EXPECTED_HOST}/kuponanaliziivv7?sslmode=require"
KEY = "local-test-passphrase-not-a-real-secret-1234"


def test_approved_active_database_and_tls():
    check_backup_inputs(GOOD, KEY)
    check_backup_inputs(GOOD.replace("sslmode=require", "sslmode=verify-full"), KEY)


@pytest.mark.parametrize("url", [
    "",
    GOOD.replace("kuponanaliziivv7", "betappv33"),
    GOOD.replace(EXPECTED_HOST, "dpg-db39f66k1f9s738tgug0-a.frankfurt-postgres.render.com"),
    GOOD.replace("sslmode=require", "sslmode=disable"),
    GOOD.replace("sslmode=require", "sslmode=prefer"),
    GOOD + "&host=untrusted.example.org",
    GOOD + "&dbname=other_database",
    GOOD.replace("postgresql://", "https://"),
    GOOD.replace("fake-credential", ""),
    GOOD.replace("kuponanaliziivv7", "postgres"),
])
def test_wrong_database_host_or_missing_security_is_rejected(url):
    with pytest.raises(ValueError):
        check_backup_inputs(url, KEY)


def test_short_encryption_key_is_rejected():
    with pytest.raises(ValueError, match="at least 24"):
        check_backup_inputs(GOOD, "short")


def test_credentials_are_never_in_error_text():
    secret = "password-must-not-be-displayed"
    with pytest.raises(ValueError) as info:
        check_backup_inputs(GOOD.replace("kuponanaliziivv7", "old_database").replace("fake-credential", secret), KEY)
    assert secret not in str(info.value)
