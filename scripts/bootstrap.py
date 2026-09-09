"""
Bootstrap CIP against a Postgres instance in one command.

Common deployment shape (Path B in INSTALL_IN_OTHER_PROJECTS.md): one
shared Postgres instance, every project points at it via DATABASE_URL,
the registry data compounds across every project that installs CIP.

What this script does, in order:
  1. Reads DATABASE_URL (required) and TEST_DATABASE_URL (optional).
  2. Verifies the Postgres server is reachable.
  3. Creates the target databases if they don't exist AND we have
     permission — uses --admin-url when you're not connecting as the
     owner (e.g. "postgres://postgres@host/postgres" for a fresh box).
  4. Applies every migration in db/migrations/ (idempotent — the runner
     tracks a schema_migration table and skips what's applied).
  5. Optionally seeds the mixed-kinds baseline so browse_components
     returns something on first call. Skipped by default; --seed opts in.
  6. Prints a summary so the operator knows what state the DB is in.

Idempotent. Safe to cron. Safe to re-run against an already-set-up DB.

Usage:
    python -m scripts.bootstrap
    python -m scripts.bootstrap --admin-url postgresql://postgres@host/postgres
    python -m scripts.bootstrap --seed
    python -m scripts.bootstrap --verify           # read-only; no writes
    python -m scripts.bootstrap --skip-test-db     # if you don't need cip_test

Exit codes:
    0  everything succeeded
    1  a required env var is missing or a fatal DB error happened
    2  connectivity check failed (Postgres unreachable)
    3  we lack permission to create the target database and it doesn't exist
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import psycopg
from dotenv import load_dotenv

from db import migrate

load_dotenv()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

@dataclass
class DbUrl:
    scheme: str
    user: str
    password: str
    host: str
    port: int
    database: str

    @classmethod
    def parse(cls, url: str) -> "DbUrl":
        p = urlparse(url)
        return cls(
            scheme=p.scheme or "postgresql",
            user=p.username or "",
            password=p.password or "",
            host=p.hostname or "localhost",
            port=p.port or 5432,
            database=(p.path or "/").lstrip("/") or "",
        )

    def with_database(self, db: str) -> str:
        # Rebuild the URL pointing at a different database.
        netloc = ""
        if self.user:
            netloc = self.user
            if self.password:
                netloc += f":{self.password}"
            netloc += "@"
        netloc += self.host
        if self.port and self.port != 5432:
            netloc += f":{self.port}"
        return urlunparse((self.scheme, netloc, f"/{db}", "", "", ""))

    def redacted(self) -> str:
        # For log output — never print the password.
        return f"{self.scheme}://{self.user}:***@{self.host}:{self.port}/{self.database}"


# ---------------------------------------------------------------------------
# Environment + connectivity
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"error: env var {name} is not set")
    return v


def _check_connectivity(admin_url: str) -> None:
    """Try connecting to whatever URL was given. Don't run any queries."""
    try:
        with psycopg.connect(admin_url, connect_timeout=5) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as e:
        print(f"error: Postgres unreachable at "
              f"{DbUrl.parse(admin_url).redacted()}: {e}", file=sys.stderr)
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Database creation
# ---------------------------------------------------------------------------

def _database_exists(admin_url: str, dbname: str) -> bool:
    with psycopg.connect(admin_url, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,),
            )
            return cur.fetchone() is not None


def _create_database(admin_url: str, dbname: str, owner: Optional[str]) -> str:
    """Returns 'created' | 'exists'."""
    if _database_exists(admin_url, dbname):
        return "exists"
    # CREATE DATABASE can't run inside a transaction — autocommit required.
    quoted_db = '"' + dbname.replace('"', '""') + '"'
    with psycopg.connect(admin_url, autocommit=True) as c:
        with c.cursor() as cur:
            if owner:
                quoted_owner = '"' + owner.replace('"', '""') + '"'
                cur.execute(f"CREATE DATABASE {quoted_db} OWNER {quoted_owner}")
            else:
                cur.execute(f"CREATE DATABASE {quoted_db}")
    return "created"


def _admin_url_for(target: DbUrl, admin_override: Optional[str]) -> str:
    """
    Return a URL suitable for CREATE DATABASE. Prefer --admin-url if
    given; otherwise reuse the target's credentials but point at the
    'postgres' maintenance DB (default in every install).
    """
    if admin_override:
        return admin_override
    return target.with_database("postgres")


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------

def _schema_summary(url: str) -> dict:
    """Read basic state — counts of applied migrations, of capabilities, etc."""
    result = {"migrations_applied": 0, "capabilities": 0, "ingest_runs": 0}
    with psycopg.connect(url) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'schema_migration'"
            )
            if cur.fetchone()[0] == 0:
                return result
            cur.execute("SELECT COUNT(*) FROM schema_migration")
            result["migrations_applied"] = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'capability'"
            )
            if cur.fetchone()[0] == 1:
                cur.execute("SELECT COUNT(*) FROM capability")
                result["capabilities"] = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'ingest_run'"
            )
            if cur.fetchone()[0] == 1:
                cur.execute("SELECT COUNT(*) FROM ingest_run")
                result["ingest_runs"] = cur.fetchone()[0]
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_bootstrap(
    *,
    admin_url: Optional[str] = None,
    do_create: bool = True,
    do_migrate: bool = True,
    do_seed: bool = False,
    skip_test_db: bool = False,
    verify_only: bool = False,
) -> int:
    live_url = _require_env("DATABASE_URL")
    live = DbUrl.parse(live_url)
    print(f"live db: {live.redacted()}")

    test_url = os.environ.get("TEST_DATABASE_URL", "").strip() or None
    test = DbUrl.parse(test_url) if (test_url and not skip_test_db) else None
    if test:
        print(f"test db: {test.redacted()}")

    if verify_only:
        _check_connectivity(live_url)
        summary = _schema_summary(live_url)
        print()
        print("verify-only mode; no writes performed")
        print(f"  migrations_applied: {summary['migrations_applied']}")
        print(f"  capabilities:       {summary['capabilities']}")
        print(f"  ingest_runs:        {summary['ingest_runs']}")
        return 0

    admin_live = _admin_url_for(live, admin_url)
    print(f"admin channel for CREATE: {DbUrl.parse(admin_live).redacted()}")
    _check_connectivity(admin_live)

    if do_create:
        try:
            state = _create_database(admin_live, live.database, owner=live.user or None)
            print(f"live db {live.database!r}: {state}")
        except psycopg.errors.InsufficientPrivilege as e:
            print(f"error: cannot CREATE DATABASE {live.database!r} — insufficient privilege.",
                  file=sys.stderr)
            print(f"       Ask the DBA to create it, or pass --admin-url with a role that can.",
                  file=sys.stderr)
            print(f"       (detail: {e})", file=sys.stderr)
            return 3
        if test is not None:
            state = _create_database(admin_live, test.database, owner=test.user or None)
            print(f"test db {test.database!r}: {state}")
    else:
        print("skipping database creation (--skip-create)")

    if do_migrate:
        print()
        print("applying migrations to live db ...")
        migrate.up(for_tests=False)
        if test is not None:
            print()
            print("applying migrations to test db ...")
            migrate.up(for_tests=True)
    else:
        print("skipping migrations (--skip-migrate)")

    if do_seed:
        print()
        print("seeding mixed-kinds baseline ...")
        from scripts.seed_mixed_kinds import main as _seed
        _seed()

    print()
    print("bootstrap complete.")
    summary = _schema_summary(live_url)
    print(f"  migrations_applied: {summary['migrations_applied']}")
    print(f"  capabilities:       {summary['capabilities']}")
    print(f"  ingest_runs:        {summary['ingest_runs']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--admin-url", default=None,
                    help="Postgres URL used for CREATE DATABASE. Defaults to "
                         "reusing DATABASE_URL's credentials + host with the "
                         "database changed to 'postgres'.")
    ap.add_argument("--skip-create", action="store_true",
                    help="Skip the CREATE DATABASE step (target dbs already exist).")
    ap.add_argument("--skip-migrate", action="store_true",
                    help="Skip the migration step.")
    ap.add_argument("--seed", action="store_true",
                    help="Run the mixed-kinds seeder so browse_components "
                         "returns something on first call. Off by default so "
                         "shared registries don't accumulate demo rows.")
    ap.add_argument("--skip-test-db", action="store_true",
                    help="Don't create or migrate the TEST_DATABASE_URL db.")
    ap.add_argument("--verify", action="store_true",
                    help="Read-only: print current schema state without any writes.")
    args = ap.parse_args()

    return run_bootstrap(
        admin_url=args.admin_url,
        do_create=not args.skip_create,
        do_migrate=not args.skip_migrate,
        do_seed=args.seed,
        skip_test_db=args.skip_test_db,
        verify_only=args.verify,
    )


if __name__ == "__main__":
    sys.exit(main())
