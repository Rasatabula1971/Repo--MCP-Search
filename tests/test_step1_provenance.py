"""
Step 1 done-when:
  "Ingesting the same repo twice produces one source_asset row and one
   source_revision row per distinct commit."

We verify this at the schema level — no ingestion code exists yet. The
uniqueness constraints are what make it true, so that's what we test.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest


def _make_provider(conn, name="github"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES (%s, %s) "
            "RETURNING id",
            (name, "code_host"),
        )
        return cur.fetchone()[0]


def _insert_asset(conn, provider_id, external_key):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_asset
              (provider_id, external_key, display_name, kind)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (provider_id, external_key, external_key, "repository"),
        )
        return cur.fetchone()[0]


def _insert_revision(conn, asset_id, revision_key):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, revision_key),
        )
        return cur.fetchone()[0]


def test_same_asset_twice_conflicts(conn):
    provider_id = _make_provider(conn)
    _insert_asset(conn, provider_id, "psf/requests")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_asset(conn, provider_id, "psf/requests")


def test_same_revision_twice_conflicts(conn):
    provider_id = _make_provider(conn)
    asset_id = _insert_asset(conn, provider_id, "psf/requests")
    _insert_revision(conn, asset_id, "abc123")
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_revision(conn, asset_id, "abc123")


def test_two_distinct_revisions_of_one_asset_coexist(conn):
    """The core claim: one asset, many revisions."""
    provider_id = _make_provider(conn)
    asset_id = _insert_asset(conn, provider_id, "psf/requests")
    _insert_revision(conn, asset_id, "commit_1")
    _insert_revision(conn, asset_id, "commit_2")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM source_revision WHERE source_asset_id = %s",
            (asset_id,),
        )
        assert cur.fetchone()[0] == 2

        cur.execute(
            "SELECT COUNT(*) FROM source_asset WHERE external_key = 'psf/requests'"
        )
        assert cur.fetchone()[0] == 1


def test_same_external_key_across_providers_is_allowed(conn):
    """Two providers, same external key = two distinct assets. This is
    what lets us track 'the same' project on GitHub and, one day, GitLab."""
    gh = _make_provider(conn, "github")
    mcp = _make_provider(conn, "mcp")
    _insert_asset(conn, gh, "someone/repo")
    _insert_asset(conn, mcp, "someone/repo")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM source_asset")
        assert cur.fetchone()[0] == 2


def test_file_artifact_unique_per_revision_path(conn):
    """Same path, same revision = one row. No duplicates from a retry."""
    provider_id = _make_provider(conn)
    asset_id = _insert_asset(conn, provider_id, "psf/requests")
    rev_id = _insert_revision(conn, asset_id, "abc123")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO file_artifact "
            "(source_revision_id, path, size_bytes, content_hash) "
            "VALUES (%s, %s, %s, %s)",
            (rev_id, "src/main.py", 42, "hash1"),
        )
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO file_artifact "
                "(source_revision_id, path, size_bytes, content_hash) "
                "VALUES (%s, %s, %s, %s)",
                (rev_id, "src/main.py", 42, "hash1"),
            )
