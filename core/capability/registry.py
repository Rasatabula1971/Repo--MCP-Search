"""
Capability registry — Step 8.

One entry point: promote_revision(). Takes an analysed source_revision
and turns it into:
  - a capability row (existing or new — keyed on normalized_key)
  - a capability_version row (keyed on content_hash within capability)
  - a capability_source_binding row (this version came from this revision)
  - capability_interface rows (one per InterfaceExtractor evidence item)
  - capability_dependency rows (one per ManifestExtractor evidence item)

Supersession: if this revision's source_asset already has a prior
capability_version pointing at it (an earlier commit of the same repo),
we set that prior version's superseded_by_id to the new one. Only
within a lineage — competing implementations from different repos
don't auto-supersede each other.

Idempotent: re-promoting the same (revision, extractor_config_version)
produces no new rows. This matches the Step 6 no-op-analysis discipline
and lets the workflow re-drive promotion safely on retry.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg

from core.capability.normalize import (
    NormalizedIdentity,
    normalize_from_files,
)
from connectors.base import AssetRef, ConnectorRegistry, RevisionKey


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionOutcome:
    capability_id: uuid.UUID
    capability_version_id: uuid.UUID
    normalized_key: str
    was_noop: bool               # True iff we short-circuited on prior binding
    superseded_prior: uuid.UUID | None
    interface_count: int
    dependency_count: int


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def promote_revision(
    conn: psycopg.Connection,
    *,
    registry: ConnectorRegistry,
    source_revision_id: uuid.UUID,
) -> PromotionOutcome:
    """
    Promote a snapshotted, analysed revision into the capability registry.
    Caller manages the transaction; we commit at each meaningful step.
    """
    rev = _load_revision_with_source(conn, source_revision_id)
    if rev["snapshot_status"] != "snapshotted":
        raise ValueError(
            f"revision {source_revision_id} is not snapshotted "
            f"(status={rev['snapshot_status']!r}) — cannot promote"
        )
    if rev["content_hash"] is None:
        raise ValueError(
            f"revision {source_revision_id} has no content_hash — "
            f"analysis must complete before promotion"
        )

    # Short-circuit: this revision is already bound to a capability_version.
    # Nothing to do — promotion is idempotent per revision.
    prior_binding = _find_existing_binding(conn, source_revision_id)
    if prior_binding is not None:
        v = _load_version(conn, prior_binding)
        return PromotionOutcome(
            capability_id=v["capability_id"],
            capability_version_id=v["id"],
            normalized_key=v["normalized_key"],
            was_noop=True,
            superseded_prior=None,
            interface_count=_count_interfaces(conn, v["id"]),
            dependency_count=_count_dependencies(conn, v["id"]),
        )

    # Fetch just enough source content to normalize — we need the
    # manifest files if they exist. Do the whole snapshot; the connector
    # already knows how to be cheap for its side.
    files_map = _fetch_manifest_files(conn, rev, registry)

    identity = normalize_from_files(
        provider_name=rev["provider_name"],
        external_key=rev["external_key"],
        files=files_map,
    )

    capability_id = _upsert_capability(conn, identity, rev)
    version_id = _create_version(conn, capability_id, rev, identity)
    _create_binding(conn, version_id, source_revision_id)

    interface_count = _link_interfaces(conn, version_id, source_revision_id)
    dependency_count = _link_dependencies(conn, version_id, source_revision_id)

    superseded_prior = _apply_supersession(
        conn,
        capability_id=capability_id,
        source_asset_id=rev["source_asset_id"],
        new_version_id=version_id,
    )

    return PromotionOutcome(
        capability_id=capability_id,
        capability_version_id=version_id,
        normalized_key=identity.normalized_key,
        was_noop=False,
        superseded_prior=superseded_prior,
        interface_count=interface_count,
        dependency_count=dependency_count,
    )


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------

def _load_revision_with_source(conn, rev_id: uuid.UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.revision_key, r.snapshot_status, r.content_hash,
                   r.source_asset_id,
                   a.external_key, a.display_name, a.kind AS asset_kind,
                   p.name AS provider_name
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
        return dict(zip([d.name for d in cur.description], row))


def _find_existing_binding(conn, rev_id: uuid.UUID) -> uuid.UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability_version_id FROM capability_source_binding "
            "WHERE source_revision_id = %s LIMIT 1",
            (str(rev_id),),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _load_version(conn, version_id: uuid.UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.capability_id, c.normalized_key
            FROM capability_version v
            JOIN capability c ON c.id = v.capability_id
            WHERE v.id = %s
            """,
            (str(version_id),),
        )
        row = cur.fetchone()
        return dict(zip([d.name for d in cur.description], row))


def _fetch_manifest_files(
    conn, rev: dict, registry: ConnectorRegistry
) -> dict[str, bytes]:
    """
    We call the connector's snapshot() once and keep only the small set
    of files normalize_from_files() cares about. Everything else is
    dropped from memory before returning.
    """
    asset = AssetRef(
        provider_name=rev["provider_name"],
        external_key=rev["external_key"],
        kind=rev["asset_kind"],
    )
    connector = registry.get(rev["provider_name"])
    wanted_basenames = ("pyproject.toml", "package.json")
    out: dict[str, bytes] = {}
    for entry in connector.snapshot(asset, RevisionKey(rev["revision_key"])):
        basename = entry.path.rsplit("/", 1)[-1]
        if basename in wanted_basenames:
            out[entry.path] = entry.content
    return out


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _upsert_capability(
    conn, identity: NormalizedIdentity, rev: dict
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO capability
              (normalized_key, display_name, ecosystem, kind, metadata)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (normalized_key) DO UPDATE
              SET display_name = capability.display_name  -- keep original
            RETURNING id
            """,
            (
                identity.normalized_key,
                identity.display_name,
                identity.ecosystem,
                identity.kind,
                json.dumps({"first_seen_from": rev["external_key"]}),
            ),
        )
        return cur.fetchone()[0]


def _create_version(
    conn,
    capability_id: uuid.UUID,
    rev: dict,
    identity: NormalizedIdentity,
) -> uuid.UUID:
    """
    Create a capability_version keyed on the revision's content_hash.
    Two commits with different bytes → two versions. Re-promoting the
    same revision (idempotency) → same version_key → ON CONFLICT hits
    and we return the existing id.
    """
    version_key = f"content:{rev['content_hash'][:16]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO capability_version
              (capability_id, version_key, version_kind, display_version, metadata)
            VALUES (%s, %s, 'content-hash', NULL, %s::jsonb)
            ON CONFLICT (capability_id, version_key) DO UPDATE
              SET metadata = capability_version.metadata
            RETURNING id
            """,
            (
                str(capability_id),
                version_key,
                json.dumps({"source_revision": rev["revision_key"]}),
            ),
        )
        return cur.fetchone()[0]


def _create_binding(
    conn, version_id: uuid.UUID, source_revision_id: uuid.UUID
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO capability_source_binding
              (capability_version_id, source_revision_id, path_scope)
            VALUES (%s, %s, '')
            ON CONFLICT (capability_version_id, source_revision_id, path_scope)
              DO NOTHING
            """,
            (str(version_id), str(source_revision_id)),
        )


def _link_interfaces(
    conn, version_id: uuid.UUID, source_revision_id: uuid.UUID
) -> int:
    """
    Copy each 'interface' evidence row for this revision into
    capability_interface. Fresh insert per version — a version doesn't
    inherit interfaces from an older sibling automatically.

    Deduplicated on (version, evidence_item) so re-running promotion
    is safe. The Step 6 evidence layer is the source of truth for the
    observation; this table is a query-friendly denormalization.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, extracted_value
            FROM evidence_item
            WHERE source_revision_id = %s
              AND evidence_type = 'interface'
            """,
            (str(source_revision_id),),
        )
        rows = cur.fetchall()

    count = 0
    with conn.cursor() as cur:
        for ev_id, value in rows:
            # Skip if this evidence already linked to this version.
            cur.execute(
                "SELECT 1 FROM capability_interface "
                "WHERE capability_version_id = %s AND evidence_item_id = %s",
                (str(version_id), str(ev_id)),
            )
            if cur.fetchone() is not None:
                continue
            cur.execute(
                """
                INSERT INTO capability_interface
                  (capability_version_id, evidence_item_id, kind, name,
                   signature, language)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(version_id),
                    str(ev_id),
                    value.get("kind", "unknown"),
                    value.get("name", ""),
                    value.get("signature") or "",
                    value.get("language"),
                ),
            )
            count += 1
    return count


def _link_dependencies(
    conn, version_id: uuid.UUID, source_revision_id: uuid.UUID
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, extracted_value
            FROM evidence_item
            WHERE source_revision_id = %s
              AND evidence_type = 'dependency'
            """,
            (str(source_revision_id),),
        )
        rows = cur.fetchall()

    count = 0
    with conn.cursor() as cur:
        for ev_id, value in rows:
            cur.execute(
                "SELECT 1 FROM capability_dependency "
                "WHERE capability_version_id = %s AND evidence_item_id = %s",
                (str(version_id), str(ev_id)),
            )
            if cur.fetchone() is not None:
                continue
            cur.execute(
                """
                INSERT INTO capability_dependency
                  (capability_version_id, evidence_item_id,
                   depends_on_ecosystem, depends_on_name, version_spec,
                   dep_kind)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    str(version_id),
                    str(ev_id),
                    value.get("ecosystem", "unknown"),
                    value.get("name", ""),
                    value.get("version_spec", ""),
                    value.get("kind", "runtime"),
                ),
            )
            count += 1
    return count


def _apply_supersession(
    conn,
    *,
    capability_id: uuid.UUID,
    source_asset_id: uuid.UUID,
    new_version_id: uuid.UUID,
) -> uuid.UUID | None:
    """
    Find the current head-of-lineage version for this capability that
    came from the SAME source_asset. If one exists (and it isn't the
    new version itself), point its superseded_by_id at the new version.

    Only within a lineage: two different repos publishing the same
    package name each maintain their own head — no cross-repo auto-
    supersession.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id
            FROM capability_version v
            JOIN capability_source_binding b
              ON b.capability_version_id = v.id
            JOIN source_revision r
              ON r.id = b.source_revision_id
            WHERE v.capability_id = %s
              AND r.source_asset_id = %s
              AND v.superseded_by_id IS NULL
              AND v.id != %s
            ORDER BY v.created_at DESC
            LIMIT 1
            """,
            (str(capability_id), str(source_asset_id), str(new_version_id)),
        )
        row = cur.fetchone()
        if row is None:
            return None
        prior_id = row[0]
        cur.execute(
            "UPDATE capability_version SET superseded_by_id = %s "
            "WHERE id = %s",
            (str(new_version_id), str(prior_id)),
        )
        return prior_id


def _count_interfaces(conn, version_id: uuid.UUID) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_interface "
            "WHERE capability_version_id = %s",
            (str(version_id),),
        )
        return cur.fetchone()[0]


def _count_dependencies(conn, version_id: uuid.UUID) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_dependency "
            "WHERE capability_version_id = %s",
            (str(version_id),),
        )
        return cur.fetchone()[0]
