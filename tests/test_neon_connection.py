"""Neon/libpq URL compatibility without silently dropping required channel binding."""
import pytest
from src.db import Database


def test_neon_required_channel_binding_uses_libpq_driver():
    db = Database("postgresql://user:password@host.neon.tech/app?sslmode=require&channel_binding=require")
    try:
        assert db.engine.url.drivername == "postgresql+psycopg"
        assert db.engine.url.query["channel_binding"] == "require"
        assert db.engine.url.query["sslmode"] == "require"
    finally:
        assert db.engine is not None


def test_neon_sslmode_only_keeps_asyncpg():
    db = Database("postgresql://user:password@host.neon.tech/app?sslmode=require")
    assert db.engine.url.drivername == "postgresql+asyncpg"
    assert "sslmode" not in db.engine.url.query


@pytest.mark.parametrize("dsn", [
    "postgresql://user:pass@host/app?channel_binding=require",
    "postgresql://user:pass@host/app?sslmode=disable&channel_binding=require",
    "postgresql://user:pass@host/app?sslmode=require&channel_binding=unknown",
])
def test_unsafe_or_unknown_security_options_are_rejected(dsn):
    with pytest.raises(ValueError):
        Database(dsn)
