"""
Shared ingester plumbing.

Every ingester follows the same shape:
  1. Open a run row (status='running').
  2. Look up the previous run's cursor to know "since when".
  3. Fetch source data.
  4. For each item: upsert source_asset + source_revision + capability
     via the helpers here — idempotent by construction.
  5. Track counts (new / updated / unchanged / deprecated / errors).
  6. Close the run row (status='completed', counts filled, cursor stored
     for next time).

Nothing here talks to a specific source. Ingesters compose these helpers.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

@dataclass
class IngestCounts:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    deprecated: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deprecated": self.deprecated,
            "errors": self.errors,
        }


def last_cursor(conn, source_name: str) -> str | None:
    """Cursor from the most recent COMPLETED run for this source, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM ingest_run_latest WHERE source_name = %s",
            (source_name,),
        )
        row = cur.fetchone()
    return row[0] if row else None


@contextmanager
def run(
    conn,
    source_name: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Iterator[IngestCounts]:
    """
    Context manager around one ingester execution.

    On enter: inserts a 'running' row, hands back an IngestCounts to mutate.
    On clean exit: marks 'completed', stores counts + cursor.
    On exception: marks 'failed', stores error detail, re-raises.

    The caller mutates `counts` throughout. To record a cursor for the
    next re-run, set `metadata['cursor']` on the dict passed in — the
    same dict object is used at commit time (no copy).
    """
    if metadata is None:
        metadata = {}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_run (source_name, metadata) "
            "VALUES (%s, %s::jsonb) RETURNING id",
            (source_name, json.dumps(metadata)),
        )
        run_id = cur.fetchone()[0]
    conn.commit()

    counts = IngestCounts()
    try:
        yield counts
    except Exception as e:
        # If the connection was left in an aborted-transaction state by
        # whatever raised, we can't run our finalizer without a rollback.
        try:
            conn.rollback()
        except Exception:
            pass
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingest_run SET "
                "  status = 'failed', "
                "  finished_at = now(), "
                "  counts = %s::jsonb, "
                "  error_detail = %s::jsonb "
                "WHERE id = %s",
                (json.dumps(counts.as_dict()),
                 json.dumps({"class": type(e).__name__, "message": str(e)}),
                 run_id),
            )
        conn.commit()
        raise
    else:
        cursor_value = metadata.get("cursor")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingest_run SET "
                "  status = 'completed', "
                "  finished_at = now(), "
                "  counts = %s::jsonb, "
                "  cursor = %s, "
                "  metadata = %s::jsonb "
                "WHERE id = %s",
                (json.dumps(counts.as_dict()),
                 cursor_value,
                 json.dumps(metadata),
                 run_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Provenance upsert helpers
# ---------------------------------------------------------------------------

def ensure_provider(conn, name: str, kind: str = "code_host") -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (name, kind),
        )
        return cur.fetchone()[0]


def ensure_asset(
    conn, provider_id: uuid.UUID, external_key: str,
    display_name: Optional[str] = None, kind: str = "repository",
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (provider_id, external_key) DO UPDATE "
            "  SET display_name = COALESCE(EXCLUDED.display_name, source_asset.display_name) "
            "RETURNING id",
            (provider_id, external_key, display_name or external_key, kind),
        )
        return cur.fetchone()[0]


def ensure_revision(
    conn, asset_id: uuid.UUID, revision_key: str,
) -> tuple[uuid.UUID, bool]:
    """
    Returns (revision_id, was_new). Same (asset, revision_key) doesn't
    create a new row — that's the idempotency guarantee for re-runs on
    unchanged data. A different revision_key (e.g. new SHA) does create
    a new row, preserving history.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM source_revision "
            "WHERE source_asset_id = %s AND revision_key = %s",
            (asset_id, revision_key),
        )
        row = cur.fetchone()
        if row:
            return (row[0], False)
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, revision_key),
        )
        return (cur.fetchone()[0], True)


def upsert_component(
    conn,
    *,
    normalized_key: str,
    display_name: str,
    ecosystem: str,
    capability_kind: str = "library",
    component_kind: str = "library",
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    license_spdx: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[uuid.UUID, bool, bool]:
    """
    Insert-or-update a capability row keyed on normalized_key.

    Returns (capability_id, was_new, was_updated). was_updated is True
    only if something visible actually changed on an existing row (name,
    metadata, license, etc.) — otherwise the run counts it as 'unchanged'.
    """
    metadata_json = json.dumps(metadata or {})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, display_name, component_kind, runtime, cost_tier, "
            "       license_spdx, metadata "
            "FROM capability WHERE normalized_key = %s",
            (normalized_key,),
        )
        existing = cur.fetchone()
        if existing:
            cap_id, e_display, e_kind, e_runtime, e_cost, e_license, e_meta = existing
            # For nullable fields we use COALESCE on write (keep the old
            # value when the ingester didn't offer one). Mirror that in
            # the change-detection so 'updated' means the row would
            # actually differ after the UPDATE — not just "the args are
            # not element-wise identical to what the DB holds."
            eff_runtime = runtime if runtime is not None else e_runtime
            eff_cost    = cost_tier if cost_tier is not None else e_cost
            eff_license = license_spdx if license_spdx is not None else e_license
            was_updated = (
                e_display != display_name
                or e_kind != component_kind
                or e_runtime != eff_runtime
                or e_cost != eff_cost
                or e_license != eff_license
                or e_meta != (metadata or {})
            )
            if was_updated:
                cur.execute(
                    "UPDATE capability SET "
                    "  display_name  = %s, "
                    "  component_kind = %s, "
                    "  runtime       = COALESCE(%s, runtime), "
                    "  cost_tier     = COALESCE(%s, cost_tier), "
                    "  license_spdx  = COALESCE(%s, license_spdx), "
                    "  metadata      = %s::jsonb "
                    "WHERE id = %s",
                    (display_name, component_kind, runtime, cost_tier,
                     license_spdx, metadata_json, cap_id),
                )
            return (cap_id, False, was_updated)
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, "
            " component_kind, runtime, cost_tier, license_spdx, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "RETURNING id",
            (normalized_key, display_name, ecosystem, capability_kind,
             component_kind, runtime, cost_tier, license_spdx, metadata_json),
        )
        return (cur.fetchone()[0], True, False)


def ensure_head_version_for_revision(
    conn, capability_id: uuid.UUID, revision_id: uuid.UUID,
    display_version: Optional[str] = None,
) -> tuple[uuid.UUID, bool]:
    """
    Ensures there's a capability_version bound to this revision. If one
    already exists (same revision), returns it. Otherwise creates a new
    version and binds it.

    Does NOT auto-supersede prior versions — that's judgment (Phase 3),
    not ingestion.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability_version_id FROM capability_source_binding "
            "WHERE source_revision_id = %s LIMIT 1",
            (revision_id,),
        )
        row = cur.fetchone()
        if row:
            return (row[0], False)
        version_key = f"revision:{revision_id}"
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version) "
            "VALUES (%s, %s, 'commit-derived', %s) RETURNING id",
            (capability_id, version_key, display_version),
        )
        ver_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO capability_source_binding "
            "(capability_version_id, source_revision_id) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (ver_id, revision_id),
        )
        return (ver_id, True)


# ---------------------------------------------------------------------------
# Deprecation
# ---------------------------------------------------------------------------

def mark_deprecated_metadata(conn, capability_id: uuid.UUID, reason: str) -> None:
    """
    Marks a capability's metadata with a `deprecated_reason` timestamp
    without moving the lifecycle table (which belongs to core.capability.
    lifecycle and has its own guards). Ingesters use this as a soft
    signal; lifecycle promotion is judgment's job.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability SET metadata = metadata || %s::jsonb WHERE id = %s",
            (json.dumps({"deprecated_reason": reason,
                         "deprecated_at": "now"}), capability_id),
        )
