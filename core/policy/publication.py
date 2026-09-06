"""
Publication decision — the enforcement point for "no code path publishes
a capability that fails a blocking gate".

Lives in core/policy/ because it's policy logic that both the workers/
gate orchestrator AND the core/capability/ lifecycle module need to
call. Keeping it here means core.capability.lifecycle doesn't have to
reach into workers/ (which would violate the import-linter contract).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from core.policy.gates import GateStatus, default_gates


@dataclass(frozen=True)
class BlockingFailure:
    """
    One reason publication was refused. gate_result_id lets the caller
    write a risk_acceptance targeting exactly this row.
    """
    gate_result_id: uuid.UUID
    gate_name: str
    reason: str | None


@dataclass(frozen=True)
class PublicationDecision:
    allowed: bool
    blocking_failures: list[BlockingFailure]
    warnings: list[str]


def can_publish(
    conn: psycopg.Connection,
    *,
    capability_version_id: uuid.UUID,
) -> PublicationDecision:
    """
    Decide whether the capability_version is publishable.

    Refuses if any gate_result row for this version has:
        status = 'fail' AND is_blocking = true
        AND no risk_acceptance row referencing it.

    Everything else (warnings, non-blocking fails, not_evaluated) is
    surfaced but does not block. not_evaluated gates block — a gate
    that was never run cannot be assumed passing.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.id, g.gate_name, g.status, g.is_blocking, g.reason,
                   EXISTS (
                       SELECT 1 FROM risk_acceptance ra
                       WHERE ra.gate_result_id = g.id
                   ) AS has_acceptance
            FROM gate_result g
            WHERE g.capability_version_id = %s
            ORDER BY g.gate_name
            """,
            (str(capability_version_id),),
        )
        rows = cur.fetchall()

    if not rows:
        return PublicationDecision(
            allowed=False,
            blocking_failures=[],
            warnings=["no gates have been evaluated for this version"],
        )

    blocking_failures: list[BlockingFailure] = []
    warnings: list[str] = []
    for gate_id, name, status, is_blocking, reason, has_acceptance in rows:
        if status == GateStatus.FAIL.value and is_blocking:
            if not has_acceptance:
                blocking_failures.append(BlockingFailure(
                    gate_result_id=gate_id,
                    gate_name=name,
                    reason=reason,
                ))
        elif status == GateStatus.WARN.value:
            warnings.append(f"{name}: {reason or 'warning'}")
        elif status == GateStatus.NOT_EVALUATED.value:
            blocking_failures.append(BlockingFailure(
                gate_result_id=gate_id,
                gate_name=name,
                reason="gate not evaluated",
            ))

    # Any registered gate that never produced a row means the
    # evaluation was incomplete — treat as blocking.
    seen = {r[1] for r in rows}
    for expected in default_gates():
        if expected.name not in seen:
            blocking_failures.append(BlockingFailure(
                gate_result_id=uuid.UUID(int=0),
                gate_name=expected.name,
                reason="registered gate never evaluated",
            ))

    return PublicationDecision(
        allowed=len(blocking_failures) == 0,
        blocking_failures=blocking_failures,
        warnings=warnings,
    )
