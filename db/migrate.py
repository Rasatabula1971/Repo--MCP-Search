"""
Migration runner.

Applies numbered SQL files from db/migrations/ in order, records what's
been applied in a schema_migration table, and refuses to re-apply a
migration whose file has changed. Nothing fancy: no down migrations, no
templating. Migrations are append-only artifacts, same rule as
state_transition.

Usage:
    python -m db.migrate up            # apply against DATABASE_URL
    python -m db.migrate up --test      # apply against TEST_DATABASE_URL
    python -m db.migrate status         # show which files are applied
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from db.connection import connect

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename    TEXT        PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _applied(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migration")
        return {row[0]: row[1] for row in cur.fetchall()}


def up(for_tests: bool = False) -> None:
    conn = connect(for_tests=for_tests)
    try:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL)
        conn.commit()

        applied = _applied(conn)
        for path in _list_migrations():
            fname = path.name
            checksum = _checksum(path)
            if fname in applied:
                if applied[fname] != checksum:
                    raise RuntimeError(
                        f"Migration {fname} was applied with a different "
                        f"checksum. Migrations are append-only — never edit "
                        f"an applied file; add a new one."
                    )
                print(f"  skip  {fname} (already applied)")
                continue

            print(f"  apply {fname}")
            # Force UTF-8 so migrations with non-ASCII characters
            # (em-dashes in comments, etc.) don't fail under Windows
            # cp1252 default encoding.
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migration (filename, checksum) "
                    "VALUES (%s, %s)",
                    (fname, checksum),
                )
            conn.commit()
        print("done")
    finally:
        conn.close()


def status(for_tests: bool = False) -> None:
    conn = connect(for_tests=for_tests)
    try:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL)
        conn.commit()
        applied = _applied(conn)
        for path in _list_migrations():
            mark = "✓" if path.name in applied else "·"
            print(f"  {mark} {path.name}")
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    for_tests = "--test" in argv[2:]
    if cmd == "up":
        up(for_tests=for_tests)
    elif cmd == "status":
        status(for_tests=for_tests)
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
