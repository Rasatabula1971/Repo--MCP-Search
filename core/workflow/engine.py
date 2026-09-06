"""
Workflow engine — Step 3.

Five primitives, each doing exactly one thing:

  1. attempt_transition   — state change + state_transition + outbox, one transaction
  2. submit_command       — idempotent ledger; the same command_id runs once
  3. claim_next_step      — SELECT ... FOR UPDATE SKIP LOCKED with a lease
  4. heartbeat / release  — extend or drop a lease
  5. classify_and_record  — retry classification + dead-letter path

Every subsequent step in the build plugs into these rather than implementing
its own control flow. Nothing here knows about GitHub, capabilities, or
models — that's the point.

Rules baked in:
  - No state change without its state_transition row.
  - No state change without its outbox event (when a caller supplies one).
  - Both of the above land in the same transaction as the entity update,
    or none of them do.
  - A caller passes a command_id; the handler NEVER runs twice for it.
  - A worker that dies loses its lease on expiry; the row goes back to
    the queue automatically.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

import psycopg

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ErrorClass(str, Enum):
    """Three outcomes. Do NOT collapse into one except."""
    TRANSIENT = "transient"          # retry with backoff
    INPUT     = "input"              # deterministic; do not retry
    POLICY    = "policy"             # route to human review


class StepStatus(str, Enum):
    PENDING       = "pending"
    CLAIMED       = "claimed"
    SUCCEEDED     = "succeeded"
    FAILED        = "failed"
    DEAD_LETTERED = "dead_lettered"


class CommandStatus(str, Enum):
    ACCEPTED  = "accepted"
    COMPLETED = "completed"
    FAILED    = "failed"


@dataclass(frozen=True)
class ClaimedStep:
    """What a worker gets back when it successfully claims work."""
    attempt_id: uuid.UUID
    run_id: uuid.UUID
    step_name: str
    attempt_number: int
    lease_token: uuid.UUID
    lease_expires_at: datetime


@dataclass(frozen=True)
class OutboxEvent:
    """A domain event to be published as part of a state change."""
    aggregate_kind: str
    aggregate_id: uuid.UUID
    event_type: str
    payload: dict


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# Defaults are conservative; steps can override by passing their own values
# to classify_and_record. The whole point of retry classification is that
# not every step wants the same policy.
MAX_TRANSIENT_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# 1. attempt_transition
# ---------------------------------------------------------------------------

def attempt_transition(
    conn: psycopg.Connection,
    *,
    entity_kind: str,
    entity_id: uuid.UUID,
    from_state: Optional[str],
    to_state: str,
    entity_update_sql: Optional[str] = None,
    entity_update_params: tuple = (),
    guards: Optional[dict[str, bool]] = None,
    actor: str = "system:worker",
    reason: Optional[str] = None,
    events: Optional[list[OutboxEvent]] = None,
) -> None:
    """
    Change an entity's state, record the transition, and enqueue any
    domain events — all in the caller's transaction.

    The caller passes an already-open connection; commit/rollback is the
    caller's responsibility. This is the only way to give the caller
    transactional control over the entity update (which we don't know how
    to write for arbitrary entities) alongside the transition + outbox.

    If any guard evaluates false, we raise. Guard results land in
    state_transition regardless of outcome so the audit trail records
    the check, not just the successful passes.
    """
    guards = guards or {}
    failing = [name for name, ok in guards.items() if not ok]
    if failing:
        # Record the failed attempt for the audit trail before raising.
        _insert_transition(
            conn, entity_kind, entity_id, from_state, from_state or "",
            guards, actor, f"guard_failed:{','.join(failing)}",
        )
        raise GuardFailure(f"guards failed: {failing}")

    # Optional: update the entity itself. Skip if the caller only wants
    # to record a transition (rare, but useful for initial-state inserts
    # where the row was just created above).
    if entity_update_sql:
        with conn.cursor() as cur:
            cur.execute(entity_update_sql, entity_update_params)
            if cur.rowcount != 1:
                raise TransitionFailure(
                    f"entity update affected {cur.rowcount} rows; expected 1"
                )

    _insert_transition(
        conn, entity_kind, entity_id, from_state, to_state,
        guards, actor, reason,
    )

    for ev in (events or []):
        _insert_outbox(conn, ev)


def _insert_transition(
    conn, entity_kind, entity_id, from_state, to_state,
    guards, actor, reason,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO state_transition
              (entity_kind, entity_id, from_state, to_state,
               guard_results, actor, reason)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (entity_kind, str(entity_id), from_state, to_state,
             json.dumps(guards), actor, reason),
        )


def _insert_outbox(conn, ev: OutboxEvent):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbox_event
              (aggregate_kind, aggregate_id, event_type, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (ev.aggregate_kind, str(ev.aggregate_id),
             ev.event_type, json.dumps(ev.payload)),
        )


class GuardFailure(Exception):
    """Raised when attempt_transition guards evaluate false."""


class TransitionFailure(Exception):
    """Raised when the entity update didn't affect exactly one row."""


# ---------------------------------------------------------------------------
# 2. submit_command — idempotent ledger
# ---------------------------------------------------------------------------

def submit_command(
    conn: psycopg.Connection,
    *,
    command_id: uuid.UUID,
    kind: str,
    payload: dict,
    handler: Callable[[psycopg.Connection, dict], dict],
) -> dict:
    """
    Insert-or-return on command_id. If we win the insert, run handler,
    record the result, and return it. If we lose (someone already
    submitted this command_id), return the prior result without running
    handler again.

    handler receives (conn, payload) and returns a JSON-serializable dict.
    handler must NOT commit — this function manages the transaction so
    that a handler crash rolls the ledger row back with everything else,
    letting the caller retry cleanly.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_command (command_id, kind, payload, status)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (command_id) DO NOTHING
            RETURNING command_id
            """,
            (str(command_id), kind, json.dumps(payload),
             CommandStatus.ACCEPTED.value),
        )
        inserted = cur.fetchone() is not None

    if not inserted:
        # Someone got here first. Return whatever they recorded.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, result FROM workflow_command "
                "WHERE command_id = %s",
                (str(command_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("command vanished between insert and select")
            status, result = row
            if status == CommandStatus.ACCEPTED.value:
                # In-flight elsewhere. The safe answer is to tell the caller
                # it was accepted; the effect will land exactly once.
                return {"status": "in_flight"}
            return result or {}

    # We own this command. Run the handler.
    try:
        result = handler(conn, payload)
    except Exception as exc:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_command SET status = %s, "
                "result = %s::jsonb, completed_at = now() "
                "WHERE command_id = %s",
                (CommandStatus.FAILED.value,
                 json.dumps({"error": str(exc)}),
                 str(command_id)),
            )
        raise

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_command SET status = %s, "
            "result = %s::jsonb, completed_at = now() "
            "WHERE command_id = %s",
            (CommandStatus.COMPLETED.value,
             json.dumps(result), str(command_id)),
        )
    return result


# ---------------------------------------------------------------------------
# 3. claim_next_step — the queue
# ---------------------------------------------------------------------------

DEFAULT_LEASE_SECONDS = 60


def claim_next_step(
    conn: psycopg.Connection,
    *,
    step_name: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[ClaimedStep]:
    """
    Atomically claim one pending step of the given name. Returns None if
    the queue is empty.

    A row is claimable if:
      - status = 'pending', OR
      - status = 'claimed' AND lease_expires_at < now()  (dead worker)

    SELECT ... FOR UPDATE SKIP LOCKED means concurrent workers won't
    fight over the same row.
    """
    lease_token = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH candidate AS (
                SELECT id FROM workflow_step_attempt
                WHERE step_name = %s
                  AND (status = 'pending'
                       OR (status = 'claimed'
                           AND lease_expires_at < now()))
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE workflow_step_attempt a
            SET status = 'claimed',
                lease_token = %s,
                lease_expires_at = now() + make_interval(secs => %s),
                last_heartbeat_at = now(),
                started_at = COALESCE(a.started_at, now())
            FROM candidate
            WHERE a.id = candidate.id
            RETURNING a.id, a.workflow_run_id, a.step_name, a.attempt_number,
                      a.lease_token, a.lease_expires_at
            """,
            (step_name, str(lease_token), lease_seconds),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return ClaimedStep(
        attempt_id=row[0],
        run_id=row[1],
        step_name=row[2],
        attempt_number=row[3],
        lease_token=row[4],
        lease_expires_at=row[5],
    )


def heartbeat(
    conn: psycopg.Connection,
    *,
    attempt_id: uuid.UUID,
    lease_token: uuid.UUID,
    extend_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """
    Extend a lease. Returns True on success. Returns False if the row's
    lease_token has changed — meaning another worker has taken over
    (lease expired, someone else picked it up), and this worker should
    stop doing anything with side effects.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_step_attempt
            SET last_heartbeat_at = now(),
                lease_expires_at  = now() + make_interval(secs => %s)
            WHERE id = %s AND lease_token = %s AND status = 'claimed'
            """,
            (extend_seconds, str(attempt_id), str(lease_token)),
        )
        return cur.rowcount == 1


def mark_succeeded(
    conn: psycopg.Connection,
    *,
    attempt_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> None:
    """Terminal state: work done, release lease."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_step_attempt
            SET status = 'succeeded',
                finished_at = now(),
                lease_token = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND lease_token = %s
            """,
            (str(attempt_id), str(lease_token)),
        )
        if cur.rowcount != 1:
            raise TransitionFailure(
                "mark_succeeded: lease no longer valid"
            )


# ---------------------------------------------------------------------------
# 4. Retry classification + dead-letter
# ---------------------------------------------------------------------------

def classify_and_record(
    conn: psycopg.Connection,
    *,
    attempt_id: uuid.UUID,
    lease_token: uuid.UUID,
    run_id: uuid.UUID,
    step_name: str,
    attempt_number: int,
    error_class: ErrorClass,
    error_detail: dict,
    max_transient_attempts: int = MAX_TRANSIENT_ATTEMPTS,
    replay_payload: Optional[dict] = None,
) -> None:
    """
    Mark the current attempt failed and decide what happens next:

      - TRANSIENT: enqueue a new attempt (attempt_number + 1) unless we've
        already hit max_transient_attempts, in which case dead-letter.
      - INPUT:     dead-letter immediately. Retrying won't help.
      - POLICY:    dead-letter with a review reason. Human gate.

    All in the caller's transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_step_attempt
            SET status = 'failed',
                finished_at = now(),
                error_class = %s,
                error_detail = %s::jsonb,
                lease_token = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND lease_token = %s
            """,
            (error_class.value, json.dumps(error_detail),
             str(attempt_id), str(lease_token)),
        )
        if cur.rowcount != 1:
            raise TransitionFailure(
                "classify_and_record: lease no longer valid"
            )

    if error_class == ErrorClass.TRANSIENT and attempt_number < max_transient_attempts:
        # Enqueue a fresh attempt.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_step_attempt
                  (workflow_run_id, step_name, attempt_number, status)
                VALUES (%s, %s, %s, 'pending')
                """,
                (str(run_id), step_name, attempt_number + 1),
            )
        return

    # Dead-letter.
    reason = {
        ErrorClass.TRANSIENT: "transient_exhausted",
        ErrorClass.INPUT:     "input_failure",
        ErrorClass.POLICY:    "policy_failure",
    }[error_class]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dead_letter_item
              (workflow_step_attempt_id, reason, detail, replay_payload)
            VALUES (%s, %s, %s::jsonb, %s::jsonb)
            """,
            (str(attempt_id), reason,
             json.dumps(error_detail),
             json.dumps(replay_payload or {})),
        )
        cur.execute(
            "UPDATE workflow_step_attempt SET status = 'dead_lettered' "
            "WHERE id = %s",
            (str(attempt_id),),
        )


# ---------------------------------------------------------------------------
# Helpers for tests and callers to start work
# ---------------------------------------------------------------------------

def create_run(
    conn: psycopg.Connection,
    *,
    definition: str,
    definition_version: int,
    entity_kind: str,
    entity_id: uuid.UUID,
    initial_steps: list[str],
    metadata: Optional[dict] = None,
) -> uuid.UUID:
    """Create a run and enqueue its initial step attempts."""
    run_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_run
              (id, definition, definition_version, entity_kind, entity_id,
               status, started_at, metadata)
            VALUES (%s, %s, %s, %s, %s, 'running', now(), %s::jsonb)
            """,
            (str(run_id), definition, definition_version,
             entity_kind, str(entity_id),
             json.dumps(metadata or {})),
        )
        for step_name in initial_steps:
            cur.execute(
                """
                INSERT INTO workflow_step_attempt
                  (workflow_run_id, step_name, attempt_number, status)
                VALUES (%s, %s, 1, 'pending')
                """,
                (str(run_id), step_name),
            )
    return run_id
