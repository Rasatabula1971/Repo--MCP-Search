"""
Connection helper.

One responsibility: hand out a psycopg connection built from DATABASE_URL
(or TEST_DATABASE_URL when we're in a test). No pooling yet — Step 3 uses
short-lived transactions and doesn't need it. A pool arrives when the API
does at Step 14.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from dotenv import load_dotenv

load_dotenv()


def _url(for_tests: bool = False) -> str:
    key = "TEST_DATABASE_URL" if for_tests else "DATABASE_URL"
    url = os.environ.get(key)
    if not url:
        raise RuntimeError(
            f"{key} is not set. Copy .env.example to .env and fill it in."
        )
    return url


def connect(for_tests: bool = False) -> psycopg.Connection:
    """Open a new connection. Caller is responsible for closing it."""
    return psycopg.connect(_url(for_tests), autocommit=False)


@contextmanager
def transaction(for_tests: bool = False) -> Iterator[psycopg.Connection]:
    """
    Short-lived transactional block. Commits on clean exit, rolls back on
    any exception. Use this for anything the workflow engine touches — the
    transactional-outbox invariant depends on state changes and their
    events being written in the same transaction.
    """
    conn = connect(for_tests=for_tests)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
