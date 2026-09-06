"""
Analysis orchestrator — Step 6.

Runs the configured extractors against a snapshotted revision, validates
every produced evidence item against the snapshot's file set, and writes
evidence_item + analysis_stage_result rows. All idempotent under the
same discipline as ingestion:

  - Re-analysis of the same content_hash at the same extractor_config_
    version is a no-op that returns the prior run.
  - Extractor A raising doesn't stop extractor B from running — each
    has its own stage_result row.
  - Every persisted evidence_item has a locator that resolves to a
    real file in the snapshot. Bad locators raise InvalidEvidence
    BEFORE the row is written; the extractor's stage_result is marked
    failed with the reason.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Iterable

import psycopg

from analysis.evidence import (
    EvidenceItem,
    InvalidEvidence,
    SourceFile,
    validate_locator_or_raise,
)
from analysis.extractors.interfaces import InterfaceExtractor
from analysis.extractors.licenses import LicenseExtractor
from analysis.extractors.manifests import ManifestExtractor
from analysis.extractors.secrets import SecretIndicatorExtractor
from analysis.extractors.tests import TestPresenceExtractor
from connectors.base import (
    AssetRef,
    ConnectorRegistry,
    RevisionKey,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Bump this when the set of extractors or their outputs changes materially.
# The no-op short-circuit keys on (content_hash, config_version) — if a
# prior run at the same content but older version exists, we still re-run.
EXTRACTOR_CONFIG_VERSION = 1


def default_extractors() -> list:
    """The stock lineup. Callers can pass a different list for tests."""
    return [
        ManifestExtractor(),
        InterfaceExtractor(),
        TestPresenceExtractor(),
        LicenseExtractor(),
        SecretIndicatorExtractor(),
    ]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageOutcome:
    extractor_name: str
    status: str                         # succeeded / failed
    evidence_count: int
    error_detail: dict | None = None


@dataclass(frozen=True)
class AnalysisOutcome:
    revision_id: uuid.UUID
    analysis_run_id: uuid.UUID
    was_noop: bool
    total_evidence_count: int
    stages: list[StageOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_analysis(
    conn: psycopg.Connection,
    *,
    registry: ConnectorRegistry,
    source_revision_id: uuid.UUID,
    extractors: list | None = None,
    extractor_config_version: int = EXTRACTOR_CONFIG_VERSION,
) -> AnalysisOutcome:
    """
    Analyse a snapshotted revision. Caller manages the transaction; we
    commit at each meaningful checkpoint.
    """
    extractors = extractors if extractors is not None else default_extractors()

    rev_row = _load_revision(conn, source_revision_id)
    if rev_row["snapshot_status"] != "snapshotted":
        raise ValueError(
            f"revision {source_revision_id} is not snapshotted "
            f"(status={rev_row['snapshot_status']}) — cannot analyse"
        )

    # No-op short-circuit: prior completed run at same config?
    prior = _find_prior_completed_run(
        conn, source_revision_id, extractor_config_version
    )
    if prior is not None:
        total = _count_evidence(conn, prior)
        return AnalysisOutcome(
            revision_id=source_revision_id,
            analysis_run_id=prior,
            was_noop=True,
            total_evidence_count=total,
            stages=_load_stage_outcomes(conn, prior),
        )

    # Create the run row.
    run_id = _create_run(conn, source_revision_id, extractor_config_version)
    conn.commit()

    # Fetch source files via the connector (one snapshot call).
    files = _fetch_files(conn, rev_row, registry)
    known_paths = {f.path for f in files}
    line_counts = {
        f.path: f.content.count(b"\n") + 1
        for f in files
        if f.language in {"python", "javascript", "typescript", "go", "rust",
                          "ruby", "java", "shell", "markdown", "yaml", "toml",
                          "json", "sql", "html", "css"}
    }

    stages: list[StageOutcome] = []
    total_evidence = 0

    for extractor in extractors:
        stage_id = _start_stage(conn, run_id, extractor.name)
        conn.commit()
        try:
            evidence_iter = extractor.extract(files)
            written = _persist_evidence(
                conn,
                run_id=run_id,
                source_revision_id=source_revision_id,
                extractor_name=extractor.name,
                items=evidence_iter,
                known_paths=known_paths,
                line_counts=line_counts,
            )
            _complete_stage(conn, stage_id, status="succeeded",
                             evidence_count=written)
            stages.append(StageOutcome(
                extractor_name=extractor.name,
                status="succeeded",
                evidence_count=written,
            ))
            total_evidence += written
            conn.commit()
        except Exception as exc:
            # Isolation: this extractor blew up, log and move on.
            conn.rollback()
            _complete_stage(
                conn, stage_id, status="failed", evidence_count=0,
                error_detail={"error": str(exc), "type": type(exc).__name__},
            )
            stages.append(StageOutcome(
                extractor_name=extractor.name,
                status="failed",
                evidence_count=0,
                error_detail={"error": str(exc)},
            ))
            conn.commit()

    _complete_run(conn, run_id, "completed")
    conn.commit()

    return AnalysisOutcome(
        revision_id=source_revision_id,
        analysis_run_id=run_id,
        was_noop=False,
        total_evidence_count=total_evidence,
        stages=stages,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_revision(conn, rev_id: uuid.UUID) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.revision_key, r.snapshot_status, r.content_hash,
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
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))


def _find_prior_completed_run(
    conn, rev_id: uuid.UUID, config_version: int
) -> uuid.UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM analysis_run
            WHERE source_revision_id = %s
              AND extractor_config_version = %s
              AND status = 'completed'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (str(rev_id), config_version),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _create_run(conn, rev_id: uuid.UUID, config_version: int) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_run
              (source_revision_id, extractor_config_version, status, started_at)
            VALUES (%s, %s, 'running', now())
            RETURNING id
            """,
            (str(rev_id), config_version),
        )
        return cur.fetchone()[0]


def _complete_run(conn, run_id: uuid.UUID, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE analysis_run SET status = %s, finished_at = now() "
            "WHERE id = %s",
            (status, str(run_id)),
        )


def _start_stage(conn, run_id: uuid.UUID, extractor_name: str) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analysis_stage_result
              (analysis_run_id, extractor_name, status, started_at)
            VALUES (%s, %s, 'running', now())
            RETURNING id
            """,
            (str(run_id), extractor_name),
        )
        return cur.fetchone()[0]


def _complete_stage(
    conn,
    stage_id: uuid.UUID,
    *,
    status: str,
    evidence_count: int,
    error_detail: dict | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE analysis_stage_result
            SET status = %s, evidence_count = %s, finished_at = now(),
                error_detail = %s
            WHERE id = %s
            """,
            (
                status, evidence_count,
                json.dumps(error_detail) if error_detail else None,
                str(stage_id),
            ),
        )


def _persist_evidence(
    conn,
    *,
    run_id: uuid.UUID,
    source_revision_id: uuid.UUID,
    extractor_name: str,
    items: Iterable[EvidenceItem],
    known_paths: set[str],
    line_counts: dict[str, int],
) -> int:
    """
    Validate then insert. If any item fails validation the whole stage
    fails — the transaction rolls back and the stage_result records the
    failure. Half-written extractor output is worse than none.
    """
    count = 0
    with conn.cursor() as cur:
        for item in items:
            validate_locator_or_raise(item, known_paths, line_counts)
            cur.execute(
                """
                INSERT INTO evidence_item
                  (source_revision_id, analysis_run_id, extractor_name,
                   evidence_type, locator_kind, locator, extracted_value)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    str(source_revision_id),
                    str(run_id),
                    extractor_name,
                    item.evidence_type,
                    item.locator_kind,
                    json.dumps(item.locator),
                    json.dumps(item.extracted_value),
                ),
            )
            count += 1
    return count


def _count_evidence(conn, run_id: uuid.UUID) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM evidence_item WHERE analysis_run_id = %s",
            (str(run_id),),
        )
        return cur.fetchone()[0]


def _load_stage_outcomes(conn, run_id: uuid.UUID) -> list[StageOutcome]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extractor_name, status, evidence_count, error_detail "
            "FROM analysis_stage_result WHERE analysis_run_id = %s "
            "ORDER BY started_at",
            (str(run_id),),
        )
        return [
            StageOutcome(
                extractor_name=r[0], status=r[1],
                evidence_count=r[2], error_detail=r[3],
            )
            for r in cur.fetchall()
        ]


def _fetch_files(
    conn, rev_row: dict, registry: ConnectorRegistry
) -> list[SourceFile]:
    """One connector.snapshot() call. Attach language from file_artifact
    since the connector's FileEntry doesn't know it."""
    asset = AssetRef(
        provider_name=rev_row["provider_name"],
        external_key=rev_row["external_key"],
        kind=rev_row["asset_kind"],
    )
    conn_iface = registry.get(rev_row["provider_name"])
    entries = list(conn_iface.snapshot(
        asset, RevisionKey(rev_row["revision_key"])
    ))

    # Pull languages from file_artifact so extractors can filter cheaply.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT path, language FROM file_artifact "
            "WHERE source_revision_id = %s",
            (str(rev_row["id"]),),
        )
        lang_by_path = {p: lang for p, lang in cur.fetchall()}

    return [
        SourceFile(path=e.path, content=e.content,
                   language=lang_by_path.get(e.path))
        for e in entries
    ]
