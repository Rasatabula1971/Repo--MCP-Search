"""
Gate orchestrator — Step 10.

Two entry points:
  1. evaluate_gates(cap_version_id, gates=None) — runs every gate,
     UPSERTs one gate_result row per (cap_version, gate).
  2. can_publish(cap_version_id) — the publication guard. Returns
     PublicationDecision(allowed=..., blocking_failures=...). Any
     failed blocking gate that lacks a matching risk_acceptance row
     means allowed=False.

The Step 10 done-when — "a repository with an incompatible license
cannot reach CATALOGED by any code path" — is implemented as: Step 11's
lifecycle transitions call can_publish() before promoting to CATALOGED
and refuse if allowed=False. Step 11 hasn't been built yet; this
module ships the guard so Step 11 has nothing to skip.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg

from core.policy.gates import (
    Gate,
    GateContext,
    GateEvidence,
    GateStatus,
    default_gates,
)
# Re-export the publication guard so existing callers keep working.
# The implementation lives in core/policy/publication.py because both
# workers/gates.py AND core/capability/lifecycle.py need to call it,
# and core/ can't reach into workers/.
from core.policy.publication import (  # noqa: F401
    BlockingFailure,
    PublicationDecision,
    can_publish,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateOutcome:
    gate_name: str
    status: GateStatus
    is_blocking: bool
    reason: str | None
    detail: dict


@dataclass(frozen=True)
class EvaluationOutcome:
    capability_version_id: uuid.UUID
    gates: list[GateOutcome]


# ---------------------------------------------------------------------------
# evaluate_gates
# ---------------------------------------------------------------------------

def evaluate_gates(
    conn: psycopg.Connection,
    *,
    capability_version_id: uuid.UUID,
    gates: list[Gate] | None = None,
) -> EvaluationOutcome:
    """
    Run every gate against the capability_version's evidence + context,
    UPSERT one gate_result row per gate. Caller commits.
    """
    gates = gates if gates is not None else default_gates()

    evidence = _load_evidence(conn, capability_version_id)
    context = _load_context(conn, capability_version_id)

    outcomes: list[GateOutcome] = []
    with conn.cursor() as cur:
        for gate in gates:
            ev_result = gate.evaluate(evidence, context)
            cur.execute(
                """
                INSERT INTO gate_result
                  (capability_version_id, gate_name, status, is_blocking,
                   reason, detail, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (capability_version_id, gate_name) DO UPDATE
                  SET status = EXCLUDED.status,
                      is_blocking = EXCLUDED.is_blocking,
                      reason = EXCLUDED.reason,
                      detail = EXCLUDED.detail,
                      evaluated_at = now()
                """,
                (
                    str(capability_version_id), gate.name,
                    ev_result.status.value, gate.is_blocking,
                    ev_result.reason, json.dumps(ev_result.detail),
                ),
            )
            outcomes.append(GateOutcome(
                gate_name=gate.name,
                status=ev_result.status,
                is_blocking=gate.is_blocking,
                reason=ev_result.reason,
                detail=ev_result.detail,
            ))

    return EvaluationOutcome(
        capability_version_id=capability_version_id,
        gates=outcomes,
    )


# ---------------------------------------------------------------------------
# accept_risk — the only way to override a failed blocking gate
# ---------------------------------------------------------------------------

def accept_risk(
    conn: psycopg.Connection,
    *,
    gate_result_id: uuid.UUID,
    accepted_by: str,
    reason: str,
    metadata: dict | None = None,
) -> uuid.UUID:
    """
    Create a risk_acceptance row for a failed blocking gate.

    reason must be non-empty at the CHECK-constraint level; we also
    strip whitespace before insert so " " isn't accepted.
    """
    if not accepted_by.strip():
        raise ValueError("accepted_by must be non-empty")
    if not reason.strip():
        raise ValueError("reason must be non-empty — audit trail requires it")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_acceptance
              (gate_result_id, accepted_by, reason, metadata)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (str(gate_result_id), accepted_by.strip(), reason.strip(),
             json.dumps(metadata or {})),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------

def _load_evidence(
    conn, capability_version_id: uuid.UUID
) -> list[GateEvidence]:
    """
    Same view onto evidence as the scorer uses — join via source_binding.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ei.id, ei.evidence_type, ei.extracted_value
            FROM evidence_item ei
            JOIN capability_source_binding csb
              ON csb.source_revision_id = ei.source_revision_id
            WHERE csb.capability_version_id = %s
            ORDER BY ei.id
            """,
            (str(capability_version_id),),
        )
        rows = cur.fetchall()
    return [
        GateEvidence(id=str(r[0]), evidence_type=r[1], extracted_value=r[2])
        for r in rows
    ]


def _load_context(conn, capability_version_id: uuid.UUID) -> GateContext:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT csb.source_revision_id, sr.content_hash
            FROM capability_source_binding csb
            JOIN source_revision sr ON sr.id = csb.source_revision_id
            WHERE csb.capability_version_id = %s
            LIMIT 1
            """,
            (str(capability_version_id),),
        )
        row = cur.fetchone()
    return GateContext(
        has_source_binding=row is not None,
        content_hash=row[1] if row else None,
    )
