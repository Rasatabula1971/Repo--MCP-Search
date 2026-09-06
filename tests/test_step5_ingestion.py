"""
Step 5 done-when:
  "Re-ingesting an unchanged revision is a no-op that costs one API call."

Plus the state-machine invariants:
  - identified -> snapshot_queued -> fetching -> hashing -> indexing ->
    snapshotted, in that order.
  - Every transition leaves a state_transition row.
  - The final transition emits a revision.snapshotted outbox event.
  - file_artifact rows carry (path, size, language, hash) — never content.
  - content_hash on the revision is deterministic given identical file
    contents, regardless of connector yield order.
  - Blocked / unavailable errors from the connector land the revision in
    the correct terminal state.
  - A crash mid-pipeline leaves a resumable checkpoint; the next call
    doesn't duplicate side effects.
"""
from __future__ import annotations

import uuid

import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from db.connection import connect
from workers.ingestion import (
    S_HASHING,
    S_INDEXING,
    S_SNAPSHOTTED,
    S_UNAVAILABLE,
    S_BLOCKED,
    ingest_revision,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


@pytest.fixture
def make_revision(conn):
    """Insert provider + asset + revision; return the revision UUID and
    an updater that lets us seed the connector to match."""
    def _make(external_key: str, revision_key: str):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_provider (name, kind) "
                "VALUES ('fake', 'code_host') "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            )
            provider_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO source_asset "
                "(provider_id, external_key, display_name, kind) "
                "VALUES (%s, %s, %s, 'repository') RETURNING id",
                (provider_id, external_key, external_key),
            )
            asset_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO source_revision (source_asset_id, revision_key) "
                "VALUES (%s, %s) RETURNING id",
                (asset_id, revision_key),
            )
            rev_id = cur.fetchone()[0]
        conn.commit()
        return rev_id
    return _make


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_walks_state_machine_end_to_end(conn, registry, make_revision):
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {
        "src/main.py": b"print('hi')\n",
        "README.md": b"# hello\n",
    })
    rev_id = make_revision("x/y", "sha_1")

    outcome = ingest_revision(
        conn, registry=registry, source_revision_id=rev_id
    )

    assert outcome.final_state == S_SNAPSHOTTED
    assert outcome.was_noop is False
    assert outcome.file_count == 2
    assert outcome.content_hash is not None

    # State transitions were recorded in the right order.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state FROM state_transition "
            "WHERE entity_kind = 'source_revision' AND entity_id = %s "
            "ORDER BY occurred_at",
            (str(rev_id),),
        )
        transitions = cur.fetchall()

    # Every step from identified to snapshotted
    expected = [
        ("identified", "snapshot_queued"),
        ("snapshot_queued", "fetching"),
        ("fetching", "hashing"),
        ("hashing", "indexing"),
        ("indexing", "snapshotted"),
    ]
    assert transitions == expected

    # Outbox event emitted at snapshotted
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload::text FROM outbox_event "
            "WHERE aggregate_id = %s",
            (str(rev_id),),
        )
        events = cur.fetchall()
    assert len(events) == 1
    assert events[0][0] == "revision.snapshotted"
    assert "file_count" in events[0][1]

    # file_artifact rows — path, size, language, hash; NO content column
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, size_bytes, language, content_hash "
            "FROM file_artifact WHERE source_revision_id = %s "
            "ORDER BY path",
            (str(rev_id),),
        )
        rows = cur.fetchall()
    assert rows[0][0] == "README.md"
    assert rows[0][2] == "markdown"
    assert rows[1][0] == "src/main.py"
    assert rows[1][2] == "python"
    # Hashes are stable, non-empty
    assert all(len(r[3]) == 64 for r in rows)


# ---------------------------------------------------------------------------
# The Step 5 "done when" bar
# ---------------------------------------------------------------------------

def test_reingesting_unchanged_revision_is_noop_with_one_connector_call(
    conn, registry, make_revision
):
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"a.py": b"1", "b.py": b"2"})
    rev_id = make_revision("x/y", "sha_1")

    # First pass: does everything.
    first = ingest_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    assert first.final_state == S_SNAPSHOTTED
    assert first.was_noop is False
    first_hash = first.content_hash

    # Reset the fake's counters so we can measure the second call.
    fake.reset_call_counts()

    # Second pass: nothing has changed. Must be a no-op costing ONE
    # connector call (current_revision), with the same content_hash.
    second = ingest_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    assert second.was_noop is True
    assert second.final_state == S_SNAPSHOTTED
    assert second.content_hash == first_hash
    assert fake.call_counts == {
        "enumerate": 0,
        "current_revision": 1,   # the one call
        "availability": 0,
        "snapshot": 0,           # not called
    }

    # No new file_artifact rows were written (still 2, not 4).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM file_artifact WHERE source_revision_id = %s",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# content_hash determinism
# ---------------------------------------------------------------------------

def test_content_hash_is_deterministic_regardless_of_yield_order(
    conn, registry, make_revision
):
    """Two runs against the same file set produce the same content_hash,
    even if the connector yields files in a different order the second
    time. This is what makes 'content-address everything' actually work."""
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"a.py": b"aaa", "b.py": b"bbb"})
    rev1 = make_revision("x/y", "sha_1")
    first = ingest_revision(conn, registry=registry, source_revision_id=rev1)

    # Second asset, same file contents, but the fake will yield in
    # REVERSED order this time — proving _compute_revision_hash sorts
    # internally rather than relying on connector order.
    fake.add_revision("other/repo", "sha_x", {"b.py": b"bbb", "a.py": b"aaa"})
    fake.set_reverse_snapshot_order(True)
    rev2 = make_revision("other/repo", "sha_x")
    second = ingest_revision(conn, registry=registry, source_revision_id=rev2)

    assert first.content_hash == second.content_hash


def test_content_hash_changes_when_file_contents_change(
    conn, registry, make_revision
):
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"a.py": b"one"})
    rev1 = make_revision("x/y", "sha_1")
    h1 = ingest_revision(conn, registry=registry,
                          source_revision_id=rev1).content_hash

    fake.add_revision("z/w", "sha_2", {"a.py": b"two"})
    rev2 = make_revision("z/w", "sha_2")
    h2 = ingest_revision(conn, registry=registry,
                          source_revision_id=rev2).content_hash
    assert h1 != h2


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_blocked_asset_lands_in_blocked_state(conn, registry, make_revision):
    fake = registry.get("fake")
    fake.add_revision("private/repo", "sha", {"a": b"x"})
    fake.mark_blocked("private/repo")
    rev_id = make_revision("private/repo", "sha")

    outcome = ingest_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    assert outcome.final_state == S_BLOCKED
    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_status FROM source_revision WHERE id = %s",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == S_BLOCKED
        # The blocking reason was recorded on the transition row.
        cur.execute(
            "SELECT reason FROM state_transition WHERE entity_id = %s "
            "AND to_state = 'blocked'",
            (str(rev_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert "blocked" in row[0].lower()


def test_unknown_revision_lands_in_unavailable_state(
    conn, registry, make_revision
):
    """We create a source_revision row for a revision the connector
    doesn't know about — snapshot() raises InputConnectorError, we land
    in unavailable."""
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"a.py": b"x"})
    # Register x/y so current_revision works, but create the revision
    # row with a different revision_key that the fake doesn't have.
    rev_id = make_revision("x/y", "sha_MISSING")

    outcome = ingest_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    assert outcome.final_state == S_UNAVAILABLE


# ---------------------------------------------------------------------------
# Resume behavior — the Step 3 death-mid-stage invariant, applied to Step 5
# ---------------------------------------------------------------------------

def test_resume_after_crash_at_hashing_produces_same_final_state(
    conn, registry, make_revision
):
    """
    Simulate the ingestion worker dying after transitioning INTO hashing
    but before completing indexing. On a fresh call with a new connection,
    ingest_revision picks up at hashing, re-fetches, re-hashes, indexes,
    and lands at snapshotted. There is exactly one snapshotted transition
    and one outbox event.
    """
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {
        "src/x.py": b"one\n",
        "src/y.py": b"two\n",
    })
    rev_id = make_revision("x/y", "sha_1")

    # Force the revision into S_HASHING by directly advancing state.
    # This mimics a crash that landed mid-pipeline. Nothing indexed yet.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE source_revision SET snapshot_status = %s WHERE id = %s",
            (S_HASHING, str(rev_id)),
        )
    conn.commit()

    # New connection = new "process". Resume.
    fake.reset_call_counts()
    with connect(for_tests=True) as resume_conn:
        outcome = ingest_revision(
            resume_conn, registry=registry, source_revision_id=rev_id
        )
    assert outcome.final_state == S_SNAPSHOTTED
    assert outcome.file_count == 2

    # Exactly one snapshotted transition, one outbox event — not doubled.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM state_transition "
            "WHERE entity_id = %s AND to_state = 'snapshotted'",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT COUNT(*) FROM outbox_event "
            "WHERE aggregate_id = %s AND event_type = 'revision.snapshotted'",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 1

    # File artifacts written exactly once (no duplicates).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM file_artifact WHERE source_revision_id = %s",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 2


def test_resume_directly_at_indexing_still_completes(conn, registry, make_revision):
    """
    Different crash point: revision landed at S_INDEXING (hashing wrote
    content_hash and transitioned, then process died). The resume must
    re-fetch to know per-file hashes for the file_artifact rows. This is
    the case where the S_INDEXING branch's rehydration matters.
    """
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"a.py": b"1", "b.py": b"2"})
    rev_id = make_revision("x/y", "sha_1")

    # Land the revision directly at S_INDEXING with a plausible
    # content_hash already written (as hashing would have done).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE source_revision "
            "SET snapshot_status = %s, content_hash = %s WHERE id = %s",
            (S_INDEXING, "fake_prewritten_hash", str(rev_id)),
        )
    conn.commit()

    fake.reset_call_counts()
    with connect(for_tests=True) as resume_conn:
        outcome = ingest_revision(
            resume_conn, registry=registry, source_revision_id=rev_id
        )

    assert outcome.final_state == S_SNAPSHOTTED
    assert outcome.file_count == 2
    # The S_INDEXING branch had to fetch to know per-file hashes.
    assert fake.call_counts["snapshot"] == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM file_artifact WHERE source_revision_id = %s",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# The 'fetching is not executing' rule — indirect verification
# ---------------------------------------------------------------------------

def test_snapshot_stores_only_metadata_not_file_content(
    conn, registry, make_revision
):
    """We assert the file_artifact table has no content column, and
    ingestion never tries to store it. Guards against a future 'let's
    just cache the source' change that would violate the storage rule."""
    fake = registry.get("fake")
    fake.add_revision("x/y", "sha_1", {"big.py": b"x" * 10000})
    rev_id = make_revision("x/y", "sha_1")
    ingest_revision(conn, registry=registry, source_revision_id=rev_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'file_artifact' ORDER BY column_name"
        )
        cols = {row[0] for row in cur.fetchall()}
    assert "content" not in cols, (
        "file_artifact has a 'content' column — violates the storage "
        "rule that says 'store path, size, language, hash — not content'"
    )
    assert cols >= {"path", "size_bytes", "language", "content_hash"}
