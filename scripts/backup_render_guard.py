"""Fail-closed guard for backing up ONLY the approved active Render PostgreSQL.

Never prints connection URLs, usernames, passwords or passphrases.
"""
import os
from urllib.parse import unquote, urlsplit, parse_qs

EXPECTED_HOST = "dpg-dbehainb9u1s739nem0g-a.frankfurt-postgres.render.com"
EXPECTED_DATABASE = "kuponanaliziivv7"
ALLOWED_SSLMODES = {"require", "verify-ca", "verify-full"}


def check_backup_inputs(database_url: str, passphrase: str) -> None:
    if not database_url:
        raise ValueError("KUPON_BACKUP_DATABASE_URL secret is missing")
    if not passphrase or len(passphrase) < 24:
        raise ValueError("KUPON_BACKUP_PASSPHRASE secret must contain at least 24 characters")
    try:
        parsed = urlsplit(database_url)
        database = unquote(parsed.path.lstrip("/"))
        host, port = parsed.hostname, parsed.port
        user, password = parsed.username, parsed.password
        params = parse_qs(parsed.query, keep_blank_values=True)
        ssl = params.get("sslmode", ["require"])
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid database URL") from None
    if (parsed.scheme not in ("postgres", "postgresql")
            or host != EXPECTED_HOST or database != EXPECTED_DATABASE
            or port not in (None, 5432) or not user or not password):
        raise ValueError("Backup source is not the approved active Kuponanaliziivv7 database")
    if parsed.fragment or set(params) - {"sslmode", "channel_binding"}:
        raise ValueError("Unsupported PostgreSQL connection parameters")
    if len(ssl) != 1 or ssl[0] not in ALLOWED_SSLMODES:
        raise ValueError("PostgreSQL backup requires TLS sslmode=require or stronger")


if __name__ == "__main__":
    check_backup_inputs(
        os.environ.get("KUPON_BACKUP_DATABASE_URL", ""),
        os.environ.get("KUPON_BACKUP_PASSPHRASE", ""),
    )
    print("Backup configuration validated for the active Kuponanaliziivv7 database (credentials hidden).")
