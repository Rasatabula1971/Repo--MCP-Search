"""
Capability lifecycle — Step 11.

Two responsibilities:
  1. `check_publication_guards()` — returns a list of GuardFailure. One
     entry per guard that isn't satisfied. Empty list means the
     capability_version is publishable.
  2. `advance_to()` — attempts a state transition, using the Step 3
     workflow engine so the transition is audited (state_transition
     row) and events flow through the outbox.

Guards (in the order they're checked):
  1. current_revision      — no newer snapshotted revision exists for
                              the same source_asset
  2. required_scorecard    — at least one scorecard row exists
  3. confidence_threshold  — scorecard.confidence >= CONFIDENCE_THRESHOLD
  4. no_blocking_gate      — workers.gates.can_publish() allowed=True
                              (delegates the "gates evaluated + all
                              acceptable" question)
  5. source_binding        — at least one capability_source_binding row
  6. summary               — summary text is non-empty
  7. interfaces            — at least one capability_interface row

Each guard is a separate check function so tests can fail one at a time
and assert the exact reason.

CONFIDENCE_THRESHOLD is hardcoded for MVP. Bumping it later is a Step
15+ tuning knob (a scoring-profile-level setting).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import psycopg


CONFIDENCE_THRESHOLD = 0.6      # MVP tuning knob


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

class LifecycleState(str, Enum):
    CANDIDATE   = "candidate"
    ANALYZED    = "analyzed"
    VERIFIED    = "verified"
    CATALOGED   = "cataloged"
    STALE       = "stale"
    QUARANTINED = "quarantined"
    DEPRECATED  = "deprecated"
    REVOKED     = "revoked"


# Allowed forward transitions. Other transitions (stale/quarantined/
# deprecated/revoked) are set explicitly by other workflows, not via
# advance_to().
_FORWARD_TRANSITIONS = {
    LifecycleState.CANDIDATE: LifecycleState.ANALYZED,
    LifecycleState.ANALYZED:  LifecycleState.VERIFIED,
    LifecycleState.VERIFIED:  LifecycleState.CATALOGED,
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GuardFailure:
    guard_name: str
    reason: str
    detail: dict


@dataclass(frozen=True)
class AdvanceOutcome:
    capability_version_id: uuid.UUID
    previous_state: str
    new_state: str
    was_noop: bool


class GuardsFailed(Exception):
    """
    Raised by advance_to when publication guards fail. Carries the full
    list of failures so callers can report all reasons, not just the
    first.
    """
    def __init__(self, failures: list[GuardFailure]):
        self.failures = failures
        names = [f.guard_name for f in failures]
        super().__init__(f"publication guards failed: {names}")


class IllegalTransition(Exception):
    """Raised on illegal state transitions (e.g. candidate → cataloged)."""


# ---------------------------------------------------------------------------
# Individual guards — pure functions of (conn, cap_version_id)
# ---------------------------------------------------------------------------

def guard_current_revision(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    """
    The bound revision must be the latest snapshotted revision for its
    source_asset. If a newer snapshotted revision exists we shouldn't
    publish the older one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH bound AS (
                SELECT sr.source_asset_id, sr.identified_at AS bound_at
                FROM capability_source_binding csb
                JOIN source_revision sr ON sr.id = csb.source_revision_id
                WHERE csb.capability_version_id = %s
                ORDER BY sr.identified_at DESC
                LIMIT 1
            ),
            newer AS (
                SELECT sr.id, sr.revision_key, sr.identified_at
                FROM source_revision sr, bound b
                WHERE sr.source_asset_id = b.source_asset_id
                  AND sr.identified_at > b.bound_at
                  AND sr.snapshot_status = 'snapshotted'
            )
            SELECT (SELECT COUNT(*) FROM newer) AS newer_count,
                   (SELECT revision_key FROM newer
                    ORDER BY identified_at DESC LIMIT 1) AS newest_key
            """,
            (str(cap_version_id),),
        )
        newer_count, newest_key = cur.fetchone()
    if newer_count > 0:
        return GuardFailure(
            guard_name="current_revision",
            reason=(
                f"a newer snapshotted revision exists on the same asset "
                f"({newer_count} newer revision(s); newest: {newest_key})"
            ),
            detail={"newer_count": newer_count, "newest_revision_key": newest_key},
        )
    return None


def guard_required_scorecard(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM scorecard "
            "WHERE capability_version_id = %s",
            (str(cap_version_id),),
        )
        n = cur.fetchone()[0]
    if n == 0:
        return GuardFailure(
            guard_name="required_scorecard",
            reason="no scorecard has been computed for this version",
            detail={},
        )
    return None


def guard_confidence_threshold(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    """
    Confidence uses the highest scorecard confidence across profiles.
    If any profile scored this version with confidence >= threshold,
    the guard passes.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(confidence), 0.0) FROM scorecard "
            "WHERE capability_version_id = %s",
            (str(cap_version_id),),
        )
        max_conf = float(cur.fetchone()[0])
    if max_conf < CONFIDENCE_THRESHOLD:
        return GuardFailure(
            guard_name="confidence_threshold",
            reason=(
                f"confidence {max_conf:.3f} below threshold "
                f"{CONFIDENCE_THRESHOLD:.3f}"
            ),
            detail={
                "max_confidence": max_conf,
                "threshold": CONFIDENCE_THRESHOLD,
            },
        )
    return None


def guard_no_blocking_gate(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    """
    Delegates to Step 10's publication guard. This is the single choke
    point that makes 'incompatible license cannot reach CATALOGED by any
    code path' true — every path to cataloged goes through advance_to,
    which calls this guard, which calls can_publish.
    """
    from core.policy.publication import can_publish

    decision = can_publish(conn, capability_version_id=cap_version_id)
    if decision.allowed:
        return None
    return GuardFailure(
        guard_name="no_blocking_gate",
        reason=(
            f"{len(decision.blocking_failures)} blocking gate failure(s): "
            f"{sorted(f.gate_name for f in decision.blocking_failures)}"
        ),
        detail={
            "blocking_failures": [
                {"gate_name": f.gate_name, "reason": f.reason}
                for f in decision.blocking_failures
            ],
            "warnings": decision.warnings,
        },
    )


def guard_source_binding(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_source_binding "
            "WHERE capability_version_id = %s",
            (str(cap_version_id),),
        )
        n = cur.fetchone()[0]
    if n == 0:
        return GuardFailure(
            guard_name="source_binding",
            reason="no capability_source_binding rows for this version",
            detail={},
        )
    return None


def guard_summary(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT summary FROM capability_version WHERE id = %s",
            (str(cap_version_id),),
        )
        row = cur.fetchone()
    if not row or not row[0] or not row[0].strip():
        return GuardFailure(
            guard_name="summary",
            reason="capability_version.summary is empty",
            detail={},
        )
    return None


def guard_interfaces(
    conn, cap_version_id: uuid.UUID
) -> GuardFailure | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_interface "
            "WHERE capability_version_id = %s",
            (str(cap_version_id),),
        )
        n = cur.fetchone()[0]
    if n == 0:
        return GuardFailure(
            guard_name="interfaces",
            reason="no capability_interface rows for this version",
            detail={},
        )
    return None


# The seven guards in the order they're evaluated.
PUBLICATION_GUARDS: list[
    Callable[[psycopg.Connection, uuid.UUID], GuardFailure | None]
] = [
    guard_current_revision,
    guard_required_scorecard,
    guard_confidence_threshold,
    guard_no_blocking_gate,
    guard_source_binding,
    guard_summary,
    guard_interfaces,
]


def check_publication_guards(
    conn: psycopg.Connection,
    *,
    capability_version_id: uuid.UUID,
) -> list[GuardFailure]:
    """
    Run every guard and return all failures. Empty list ⇒ publishable.
    Tests call this to assert each guard fails for its specific reason.
    """
    failures: list[GuardFailure] = []
    for guard in PUBLICATION_GUARDS:
        result = guard(conn, capability_version_id)
        if result is not None:
            failures.append(result)
    return failures


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------

def current_state(
    conn: psycopg.Connection, cap_version_id: uuid.UUID
) -> LifecycleState:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle_state FROM capability_version WHERE id = %s",
            (str(cap_version_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"capability_version {cap_version_id} not found")
        return LifecycleState(row[0])


def advance_to(
    conn: psycopg.Connection,
    *,
    capability_version_id: uuid.UUID,
    target: LifecycleState,
    actor: str = "system:lifecycle",
) -> AdvanceOutcome:
    """
    Attempt a forward transition to `target`. Raises IllegalTransition
    if the current state isn't the predecessor. When advancing to
    CATALOGED, checks all seven publication guards; failure raises
    GuardsFailed with the full failure list.

    Uses the workflow engine's attempt_transition so state_transition
    rows land in the same tx as the entity update.
    """
    from core.workflow.engine import OutboxEvent, attempt_transition

    current = current_state(conn, capability_version_id)

    # No-op if already at target.
    if current == target:
        return AdvanceOutcome(
            capability_version_id=capability_version_id,
            previous_state=current.value,
            new_state=target.value,
            was_noop=True,
        )

    expected_from = _predecessor_of(target)
    if expected_from is None:
        raise IllegalTransition(
            f"{target.value!r} is not reachable via advance_to; "
            f"other transitions require an explicit workflow"
        )
    if current != expected_from:
        raise IllegalTransition(
            f"cannot advance to {target.value!r} from {current.value!r}; "
            f"expected {expected_from.value!r}"
        )

    # For CATALOGED, all seven guards must pass.
    if target == LifecycleState.CATALOGED:
        failures = check_publication_guards(
            conn, capability_version_id=capability_version_id
        )
        if failures:
            raise GuardsFailed(failures)

    # The transition itself.
    attempt_transition(
        conn,
        entity_kind="capability_version",
        entity_id=capability_version_id,
        from_state=current.value,
        to_state=target.value,
        entity_update_sql=(
            "UPDATE capability_version SET lifecycle_state = %s "
            "WHERE id = %s AND lifecycle_state = %s"
        ),
        entity_update_params=(target.value, str(capability_version_id),
                              current.value),
        actor=actor,
        reason=f"advance {current.value} → {target.value}",
        events=[OutboxEvent(
            aggregate_kind="capability_version",
            aggregate_id=capability_version_id,
            event_type=f"capability_version.{target.value}",
            payload={"capability_version_id": str(capability_version_id)},
        )],
    )

    return AdvanceOutcome(
        capability_version_id=capability_version_id,
        previous_state=current.value,
        new_state=target.value,
        was_noop=False,
    )


def _predecessor_of(target: LifecycleState) -> LifecycleState | None:
    for src, dst in _FORWARD_TRANSITIONS.items():
        if dst == target:
            return src
    return None
