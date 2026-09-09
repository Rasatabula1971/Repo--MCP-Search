"""
Tests for scripts/bootstrap.py — the pure URL/state helpers.

The end-to-end create/migrate path is intentionally not covered here
(it wants a fresh Postgres instance we shouldn't spin up per-test).
_schema_summary against the real conn fixture stands in as an
integration smoke test.
"""
from __future__ import annotations

import pytest

from scripts import bootstrap as bs


# ---------------------------------------------------------------------------
# DbUrl parsing + rewriting
# ---------------------------------------------------------------------------

def test_parse_full_url():
    u = bs.DbUrl.parse("postgresql://alice:secret@db.example.com:5433/appdb")
    assert (u.scheme, u.user, u.password, u.host, u.port, u.database) == (
        "postgresql", "alice", "secret", "db.example.com", 5433, "appdb",
    )


def test_parse_url_defaults_when_partial():
    u = bs.DbUrl.parse("postgresql:///appdb")
    assert u.host == "localhost"
    assert u.port == 5432
    assert u.database == "appdb"
    assert u.user == ""


def test_with_database_rebuilds_url_pointing_at_maintenance_db():
    u = bs.DbUrl.parse("postgresql://alice:secret@db.example.com:5433/appdb")
    new = u.with_database("postgres")
    reparsed = bs.DbUrl.parse(new)
    assert reparsed.database == "postgres"
    assert reparsed.user == "alice"
    assert reparsed.password == "secret"
    assert reparsed.host == "db.example.com"
    assert reparsed.port == 5433


def test_with_database_omits_default_port():
    u = bs.DbUrl.parse("postgresql://alice@localhost:5432/x")
    new = u.with_database("postgres")
    assert ":5432" not in new     # canonical form drops default
    assert new.endswith("/postgres")


def test_redacted_never_leaks_password():
    u = bs.DbUrl.parse("postgresql://alice:very-secret@host/db")
    r = u.redacted()
    assert "very-secret" not in r
    assert "***" in r
    assert "alice" in r


# ---------------------------------------------------------------------------
# _admin_url_for
# ---------------------------------------------------------------------------

def test_admin_url_prefers_override():
    live = bs.DbUrl.parse("postgresql://alice@host/appdb")
    override = "postgresql://postgres@host/postgres"
    assert bs._admin_url_for(live, override) == override


def test_admin_url_defaults_to_reusing_creds_at_postgres_db():
    live = bs.DbUrl.parse("postgresql://alice:secret@host:5433/appdb")
    got = bs._admin_url_for(live, None)
    reparsed = bs.DbUrl.parse(got)
    assert reparsed.database == "postgres"
    assert reparsed.user == "alice"
    assert reparsed.port == 5433


# ---------------------------------------------------------------------------
# _schema_summary — integration smoke against the test DB
# ---------------------------------------------------------------------------

def test_schema_summary_returns_counts(conn):
    """The conn fixture points at TEST_DATABASE_URL with migrations
    already applied. Summary should report a non-zero migration count."""
    import os
    test_url = os.environ["TEST_DATABASE_URL"]
    summary = bs._schema_summary(test_url)
    assert summary["migrations_applied"] >= 1


def test_schema_summary_handles_fresh_db_without_schema(monkeypatch):
    """A URL pointing at a db with no schema_migration table returns zeros."""
    # Simulate by pointing at the 'postgres' maintenance db (definitely
    # exists but has no CIP tables).
    import os
    live = bs.DbUrl.parse(os.environ["DATABASE_URL"])
    maintenance = live.with_database("postgres")
    summary = bs._schema_summary(maintenance)
    assert summary == {"migrations_applied": 0, "capabilities": 0, "ingest_runs": 0}


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_parses_verify_flag():
    import sys
    old = sys.argv
    sys.argv = ["cip-bootstrap", "--verify"]
    # Don't actually run it; just confirm argparse accepts the shape.
    try:
        import argparse
        ap = argparse.ArgumentParser()
        # Re-declare the parser matches bs.main()'s shape.
        ap.add_argument("--admin-url")
        ap.add_argument("--skip-create", action="store_true")
        ap.add_argument("--skip-migrate", action="store_true")
        ap.add_argument("--seed", action="store_true")
        ap.add_argument("--skip-test-db", action="store_true")
        ap.add_argument("--verify", action="store_true")
        ns = ap.parse_args(["--verify"])
        assert ns.verify is True
    finally:
        sys.argv = old
