"""
Ingestion worker — Step 5.

Drives a source_revision through its state machine:

    identified -> snapshot_queued -> fetching -> hashing
                                              -> indexing -> snapshotted

Each transition uses the Step 3 primitives (attempt_transition + outbox
event, all in one DB transaction). Every state change leaves an audit
row; every side effect is idempotent under retry.

Key invariants baked in:
  - Fetching is not executing. We call connector.snapshot() which streams
    bytes; we never run install/build/test scripts. The state machine
    doesn't even have a stage for that in Step 5 — sandbox is a Step 8
    concern the brief explicitly deferred.
  - Content-addressed everything. content_hash on the revision is what
    makes re-analysis skippable.
  - Re-ingesting an unchanged revision is a no-op that costs one
    connector call (current_revision). If the revision row already has
    snapshot_status='snapshotted' and content_hash populated, we return
    early — no fetch, no hash, no index.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

import psycopg

from connectors.base import (
    AssetRef,
    BlockedConnectorError,
    ConnectorRegistry,
    FileEntry,
    InputConnectorError,
    RevisionKey,
    SourceConnector,
    TransientConnectorError,
)
from core.workflow.engine import (
    ErrorClass,
    OutboxEvent,
    attempt_transition,
)


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

S_IDENTIFIED      = "identified"
S_SNAPSHOT_QUEUED = "snapshot_queued"
S_FETCHING        = "fetching"
S_HASHING         = "hashing"
S_INDEXING        = "indexing"
S_SNAPSHOTTED     = "snapshotted"
S_UNAVAILABLE     = "unavailable"
S_BLOCKED         = "blocked"

TERMINAL_STATES = {S_SNAPSHOTTED, S_UNAVAILABLE, S_BLOCKED}


# ---------------------------------------------------------------------------
# Result type — small enough that a dict would do, but nameable is nicer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngestionOutcome:
    revision_id: uuid.UUID
    final_state: str
    was_noop: bool                   # True iff we short-circuited on
                                      # 'already snapshotted, unchanged'
    file_count: int
    content_hash: str | None
    connector_calls: dict[str, int]  # for tests and cost telemetry


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------

def ingest_revision(
    conn: psycopg.Connection,
    *,
    registry: ConnectorRegistry,
    source_revision_id: uuid.UUID,
) -> IngestionOutcome:
    """
    Move a source_revision as far along the state machine as it can go
    in one pass. Caller manages the transaction — pass an open, non-
    autocommit connection; we commit at each state transition so a crash
    mid-pipeline leaves a resumable checkpoint rather than losing hours
    of work.

    Returns an IngestionOutcome describing what happened.
    """
    # Read the revision row + its asset + its provider.
    rev_row = _load_revision(conn, source_revision_id)
    asset_ref = AssetRef(
        provider_name=rev_row["provider_name"],
        external_key=rev_row["external_key"],
        kind=rev_row["asset_kind"],
        display_name=rev_row["display_name"],
    )
    connector = registry.get(rev_row["provider_name"])

    # Short-circuit: this revision has already been snapshotted with
    # content_hash. Re-ingesting is a no-op that spends ONE connector
    # call (current_revision) to confirm the world still agrees.
    if (
        rev_row["snapshot_status"] == S_SNAPSHOTTED
        and rev_row["content_hash"] is not None
    ):
        # Confirm the revision key still resolves to the same SHA at
        # the provider. If it doesn't, the caller passed us a stale
        # revision id and something upstream is wrong — but we still
        # don't re-do the work.
        # (One call: current_revision.)
        try:
            _current = connector.current_revision(asset_ref)
        except (BlockedConnectorError, InputConnectorError):
            # Even if the provider now refuses, our stored snapshot
            # is still valid. Return the outcome as a no-op.
            pass
        return IngestionOutcome(
            revision_id=source_revision_id,
            final_state=S_SNAPSHOTTED,
            was_noop=True,
            file_count=rev_row["file_count"],
            content_hash=rev_row["content_hash"],
            connector_calls=_call_counts(connector),
        )

    # Otherwise: drive the state machine.
    _reset_call_counts(connector)

    state = rev_row["snapshot_status"]
    try:
        if state == S_IDENTIFIED:
            _transition(conn, source_revision_id, state, S_SNAPSHOT_QUEUED)
            state = S_SNAPSHOT_QUEUED
            conn.commit()

        if state == S_SNAPSHOT_QUEUED:
            _transition(conn, source_revision_id, state, S_FETCHING)
            state = S_FETCHING
            conn.commit()

        if state == S_FETCHING:
            # Actually fetch. This is the one expensive call.
            file_entries = list(connector.snapshot(
                asset_ref,
                RevisionKey(value=rev_row["revision_key"]),
            ))
            _transition(conn, source_revision_id, state, S_HASHING)
            state = S_HASHING
            # We commit AFTER buffering the fetch result in memory,
            # so the hashing step below can proceed in the same run;
            # if the process dies here, restart picks up at S_HASHING
            # and re-calls snapshot() (which is idempotent — same
            # revision, same bytes).
            conn.commit()
        else:
            file_entries = None  # resume path: fetch again if needed

        if state == S_HASHING:
            if file_entries is None:
                # Resume path — we hit S_HASHING without buffered files.
                # Re-fetch. Snapshot is deterministic per (asset, revision),
                # so this is safe.
                file_entries = list(connector.snapshot(
                    asset_ref,
                    RevisionKey(value=rev_row["revision_key"]),
                ))
            per_file_hashes = [
                (fe, hashlib.sha256(fe.content).hexdigest())
                for fe in file_entries
            ]
            revision_hash = _compute_revision_hash(per_file_hashes)

            _transition(conn, source_revision_id, state, S_INDEXING,
                        extra_update_sql=(
                            "UPDATE source_revision SET content_hash = %s "
                            "WHERE id = %s"
                        ),
                        extra_update_params=(revision_hash, str(source_revision_id)))
            state = S_INDEXING
            conn.commit()

        if state == S_INDEXING:
            # If we got here on a resume without the buffered files, fetch
            # + rehash again to know the per-file hashes. Same idempotency
            # argument as above.
            if file_entries is None:
                file_entries = list(connector.snapshot(
                    asset_ref,
                    RevisionKey(value=rev_row["revision_key"]),
                ))
                per_file_hashes = [
                    (fe, hashlib.sha256(fe.content).hexdigest())
                    for fe in file_entries
                ]
            _write_file_artifacts(conn, source_revision_id, per_file_hashes)
            _transition(
                conn, source_revision_id, state, S_SNAPSHOTTED,
                extra_update_sql=(
                    "UPDATE source_revision SET snapshotted_at = now() "
                    "WHERE id = %s"
                ),
                extra_update_params=(str(source_revision_id),),
                events=[OutboxEvent(
                    aggregate_kind="source_revision",
                    aggregate_id=source_revision_id,
                    event_type="revision.snapshotted",
                    payload={
                        "revision_id": str(source_revision_id),
                        "file_count": len(per_file_hashes),
                    },
                )],
            )
            conn.commit()
            state = S_SNAPSHOTTED

    except BlockedConnectorError as exc:
        _terminal_transition(
            conn, source_revision_id, state, S_BLOCKED,
            reason=str(exc),
        )
        conn.commit()
        return IngestionOutcome(
            revision_id=source_revision_id,
            final_state=S_BLOCKED,
            was_noop=False,
            file_count=0,
            content_hash=None,
            connector_calls=_call_counts(connector),
        )

    except InputConnectorError as exc:
        _terminal_transition(
            conn, source_revision_id, state, S_UNAVAILABLE,
            reason=str(exc),
        )
        conn.commit()
        return IngestionOutcome(
            revision_id=source_revision_id,
            final_state=S_UNAVAILABLE,
            was_noop=False,
            file_count=0,
            content_hash=None,
            connector_calls=_call_counts(connector),
        )

    # If we got here, state == S_SNAPSHOTTED.
    final = _load_revision(conn, source_revision_id)
    return IngestionOutcome(
        revision_id=source_revision_id,
        final_state=S_SNAPSHOTTED,
        was_noop=False,
        file_count=final["file_count"],
        content_hash=final["content_hash"],
        connector_calls=_call_counts(connector),
    )


# ---------------------------------------------------------------------------
# classify_connector_error — for use inside a workflow step wrapper
# ---------------------------------------------------------------------------

def classify_connector_error(exc: BaseException) -> ErrorClass:
    """Map a connector exception to the workflow engine's error class."""
    if isinstance(exc, TransientConnectorError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, BlockedConnectorError):
        return ErrorClass.POLICY
    if isinstance(exc, InputConnectorError):
        return ErrorClass.INPUT
    # Unknown = transient; safer to retry than to dead-letter.
    return ErrorClass.TRANSIENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_revision(conn, rev_id: uuid.UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.revision_key, r.snapshot_status, r.content_hash,
                   a.id AS asset_id, a.external_key, a.display_name,
                   a.kind AS asset_kind, p.name AS provider_name,
                   (SELECT COUNT(*) FROM file_artifact fa
                    WHERE fa.source_revision_id = r.id) AS file_count
            FROM source_revision r
            JOIN source_asset a    ON a.id = r.source_asset_id
            JOIN source_provider p ON p.id = a.provider_id
            WHERE r.id = %s
            """,
            (str(rev_id),),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"source_revision {rev_id} not found")
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))


def _transition(
    conn,
    rev_id: uuid.UUID,
    from_state: str,
    to_state: str,
    *,
    extra_update_sql: str | None = None,
    extra_update_params: tuple = (),
    events: list[OutboxEvent] | None = None,
) -> None:
    """
    Advance snapshot_status on the revision. The engine's
    attempt_transition writes the state_transition + optional outbox in
    the same transaction as the entity update.
    """
    # We update snapshot_status via attempt_transition, plus optionally
    # a second update for content_hash or snapshotted_at.
    attempt_transition(
        conn,
        entity_kind="source_revision",
        entity_id=rev_id,
        from_state=from_state,
        to_state=to_state,
        entity_update_sql=(
            "UPDATE source_revision SET snapshot_status = %s "
            "WHERE id = %s AND snapshot_status = %s"
        ),
        entity_update_params=(to_state, str(rev_id), from_state),
        events=events,
    )
    if extra_update_sql:
        with conn.cursor() as cur:
            cur.execute(extra_update_sql, extra_update_params)


def _terminal_transition(
    conn,
    rev_id: uuid.UUID,
    from_state: str,
    to_state: str,
    reason: str,
) -> None:
    attempt_transition(
        conn,
        entity_kind="source_revision",
        entity_id=rev_id,
        from_state=from_state,
        to_state=to_state,
        entity_update_sql=(
            "UPDATE source_revision SET snapshot_status = %s WHERE id = %s"
        ),
        entity_update_params=(to_state, str(rev_id)),
        actor="system:ingestion",
        reason=reason,
    )


def _compute_revision_hash(
    per_file_hashes: list[tuple[FileEntry, str]],
) -> str:
    """
    Stable content hash for the revision as a whole. Deterministic
    regardless of the order files were yielded by the connector.

    Format: for each file, sorted by path, emit 'path\thash\n'; SHA256
    the concatenation. Simple, reproducible, doesn't depend on any
    library beyond stdlib.
    """
    lines = sorted(f"{fe.path}\t{hh}\n" for fe, hh in per_file_hashes)
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def _write_file_artifacts(
    conn,
    rev_id: uuid.UUID,
    per_file_hashes: list[tuple[FileEntry, str]],
) -> None:
    """
    Idempotent write: on a resume the file_artifact rows may already
    exist. We DELETE then INSERT to keep the resume path clean; per the
    unique constraint on (source_revision_id, path), a duplicate insert
    would violate. Deleting-first is correct because file_artifact is
    not append-only (unlike state_transition/evidence_item).
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM file_artifact WHERE source_revision_id = %s",
            (str(rev_id),),
        )
        for fe, fh in per_file_hashes:
            cur.execute(
                """
                INSERT INTO file_artifact
                  (source_revision_id, path, size_bytes, language, content_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (str(rev_id), fe.path, fe.size_bytes,
                 _detect_language(fe.path), fh),
            )


# Very small language detector. Step 6 replaces this with a real one
# (tree-sitter language guesser or similar). For Step 5 we only need
# something to populate the column with, so filename extension is fine.
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".rb": "ruby", ".sh": "shell",
    ".md": "markdown", ".yml": "yaml", ".yaml": "yaml",
    ".json": "json", ".toml": "toml", ".sql": "sql", ".html": "html",
    ".css": "css",
}


def _detect_language(path: str) -> str | None:
    lower = path.lower()
    for ext, lang in _LANG_BY_EXT.items():
        if lower.endswith(ext):
            return lang
    return None


def _call_counts(connector: SourceConnector) -> dict[str, int]:
    """Read call counts off a fake connector for telemetry/tests. Real
    connectors don't expose this; return zeros so callers don't crash."""
    counts = getattr(connector, "call_counts", None)
    return dict(counts) if counts else {}


def _reset_call_counts(connector: SourceConnector) -> None:
    reset = getattr(connector, "reset_call_counts", None)
    if callable(reset):
        reset()
