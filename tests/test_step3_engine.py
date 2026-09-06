"""
Step 3 done-when:
  "Killing a worker mid-stage leaves the run resumable, and the resumed
   run does not duplicate its side effects."

We exercise each primitive against its specific invariant:

  1. attempt_transition:
       - transition row + entity update + outbox event land in one txn
       - a rollback loses all three together
       - a failing guard blocks the state change but records the attempt
  2. submit_command:
       - same command_id run twice = handler runs once, same result
       - handler crash rolls the ledger row back so the next call retries
  3. claim_next_step:
       - two workers competing for one row: exactly one wins
       - a dead worker's lease expires and the row goes back to the queue
  4. heartbeat:
       - a stale lease can't extend itself
  5. classify_and_record:
       - TRANSIENT under limit enqueues a fresh attempt
       - TRANSIENT at limit dead-letters
       - INPUT dead-letters immediately
       - POLICY dead-letters immediately
"""
from __future__ import annotations

import time
import uuid

import pytest

from core.workflow.engine import (
    ClaimedStep,
    ErrorClass,
    GuardFailure,
    OutboxEvent,
    attempt_transition,
    claim_next_step,
    classify_and_record,
    create_run,
    heartbeat,
    mark_succeeded,
    submit_command,
)
from db.connection import connect


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider_and_asset(conn):
    """Give us a real entity to run transitions against."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES "
            "('github', 'code_host') RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, 'psf/requests', 'psf/requests', 'repository') "
            "RETURNING id",
            (provider_id,),
        )
        asset_id = cur.fetchone()[0]
    conn.commit()
    return asset_id


def _make_revision(conn, asset_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, "abc123"),
        )
        rev_id = cur.fetchone()[0]
    conn.commit()
    return rev_id


# ---------------------------------------------------------------------------
# 1. attempt_transition
# ---------------------------------------------------------------------------

def test_attempt_transition_updates_entity_transition_and_outbox_atomically(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)

    attempt_transition(
        conn,
        entity_kind="source_revision",
        entity_id=rev_id,
        from_state="identified",
        to_state="snapshot_queued",
        entity_update_sql=(
            "UPDATE source_revision SET snapshot_status = 'snapshot_queued' "
            "WHERE id = %s AND snapshot_status = 'identified'"
        ),
        entity_update_params=(str(rev_id),),
        actor="test",
        events=[
            OutboxEvent(
                aggregate_kind="source_revision",
                aggregate_id=rev_id,
                event_type="revision.queued",
                payload={"revision_id": str(rev_id)},
            )
        ],
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT snapshot_status FROM source_revision WHERE id = %s",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == "snapshot_queued"

        cur.execute(
            "SELECT COUNT(*) FROM state_transition "
            "WHERE entity_kind = 'source_revision' AND entity_id = %s "
            "AND to_state = 'snapshot_queued'",
            (str(rev_id),),
        )
        assert cur.fetchone()[0] == 1

        cur.execute(
            "SELECT COUNT(*) FROM outbox_event "
            "WHERE event_type = 'revision.queued'"
        )
        assert cur.fetchone()[0] == 1


def test_attempt_transition_rollback_loses_all_three(conn):
    """
    The transactional-outbox promise: nothing is half-written. If the
    caller rolls back, the entity update, the transition, and the outbox
    event all disappear together.
    """
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)

    attempt_transition(
        conn,
        entity_kind="source_revision",
        entity_id=rev_id,
        from_state="identified",
        to_state="snapshot_queued",
        entity_update_sql=(
            "UPDATE source_revision SET snapshot_status = 'snapshot_queued' "
            "WHERE id = %s"
        ),
        entity_update_params=(str(rev_id),),
        events=[OutboxEvent(
            aggregate_kind="source_revision",
            aggregate_id=rev_id,
            event_type="revision.queued",
            payload={},
        )],
    )
    conn.rollback()

    # Fresh connection so we're not seeing our own uncommitted work.
    with connect(for_tests=True) as verify:
        with verify.cursor() as cur:
            cur.execute(
                "SELECT snapshot_status FROM source_revision WHERE id = %s",
                (str(rev_id),),
            )
            assert cur.fetchone()[0] == "identified"  # untouched
            cur.execute("SELECT COUNT(*) FROM state_transition")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM outbox_event")
            assert cur.fetchone()[0] == 0


def test_attempt_transition_failing_guard_blocks_change_but_records_attempt(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)

    with pytest.raises(GuardFailure):
        attempt_transition(
            conn,
            entity_kind="source_revision",
            entity_id=rev_id,
            from_state="identified",
            to_state="snapshotted",
            guards={"content_hash_present": False},
            entity_update_sql=(
                "UPDATE source_revision SET snapshot_status = 'snapshotted' "
                "WHERE id = %s"
            ),
            entity_update_params=(str(rev_id),),
        )
    conn.commit()  # commit whatever landed before the raise

    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_status FROM source_revision WHERE id = %s",
                    (str(rev_id),))
        assert cur.fetchone()[0] == "identified"  # unchanged

        cur.execute(
            "SELECT guard_results::text, reason FROM state_transition "
            "WHERE entity_id = %s",
            (str(rev_id),),
        )
        row = cur.fetchone()
        assert row is not None, "the failing attempt should be audited"
        assert "content_hash_present" in row[0]
        assert row[1].startswith("guard_failed")


# ---------------------------------------------------------------------------
# 2. submit_command
# ---------------------------------------------------------------------------

def test_submit_command_runs_handler_once_for_duplicate_command_id(conn):
    cmd = uuid.uuid4()
    call_count = {"n": 0}

    def handler(_conn, payload):
        call_count["n"] += 1
        return {"echo": payload.get("value")}

    r1 = submit_command(conn, command_id=cmd, kind="test",
                        payload={"value": 42}, handler=handler)
    conn.commit()

    r2 = submit_command(conn, command_id=cmd, kind="test",
                        payload={"value": 999}, handler=handler)
    conn.commit()

    assert r1 == {"echo": 42}
    assert r2 == {"echo": 42}, "second call must return the first result"
    assert call_count["n"] == 1


def test_submit_command_handler_crash_rolls_back_ledger_row(conn):
    """
    If the handler raises, the whole transaction rolls back — including
    the ledger row. The next call with the same command_id gets a fresh
    shot at running the handler.
    """
    cmd = uuid.uuid4()

    def bad_handler(_conn, _payload):
        raise ValueError("oops")

    with pytest.raises(ValueError):
        submit_command(conn, command_id=cmd, kind="test",
                        payload={}, handler=bad_handler)
    conn.rollback()

    # Ledger row shouldn't be there.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM workflow_command WHERE command_id = %s",
            (str(cmd),),
        )
        assert cur.fetchone()[0] == 0

    # Now a good handler should succeed cleanly.
    good_calls = {"n": 0}
    def good_handler(_conn, _payload):
        good_calls["n"] += 1
        return {"ok": True}

    result = submit_command(conn, command_id=cmd, kind="test",
                            payload={}, handler=good_handler)
    conn.commit()
    assert result == {"ok": True}
    assert good_calls["n"] == 1


# ---------------------------------------------------------------------------
# 3. claim_next_step
# ---------------------------------------------------------------------------

def test_two_workers_competing_for_one_row_only_one_wins(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    conn_a = connect(for_tests=True)
    conn_b = connect(for_tests=True)
    try:
        # Worker A opens a transaction and claims. Its FOR UPDATE lock is
        # held until commit. Worker B, running concurrently, must get None
        # because of SKIP LOCKED — it doesn't wait, it just moves on.
        claim_a = claim_next_step(conn_a, step_name="fetch")
        claim_b = claim_next_step(conn_b, step_name="fetch")
        conn_a.commit()
        conn_b.commit()

        assert claim_a is not None
        assert claim_b is None, "SKIP LOCKED means B sees nothing, not a duplicate"
    finally:
        conn_a.close()
        conn_b.close()


def test_expired_lease_returns_row_to_queue(conn):
    """
    Simulate a dead worker: set the lease_expires_at in the past. The
    next claim should pick it up.
    """
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    first = claim_next_step(conn, step_name="fetch")
    assert first is not None
    conn.commit()

    # Second claim should get nothing — lease is fresh.
    second = claim_next_step(conn, step_name="fetch")
    assert second is None
    conn.commit()

    # Now simulate lease expiry — worker died before completing.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_step_attempt "
            "SET lease_expires_at = now() - interval '1 minute' "
            "WHERE id = %s",
            (str(first.attempt_id),),
        )
    conn.commit()

    third = claim_next_step(conn, step_name="fetch")
    assert third is not None
    assert third.attempt_id == first.attempt_id, "same row, new lease"
    assert third.lease_token != first.lease_token
    conn.commit()


def test_heartbeat_from_stale_lease_holder_is_rejected(conn):
    """
    Worker A claimed, worker A's lease expired, worker B took over.
    Worker A wakes up and tries to heartbeat with its old token. That
    heartbeat must fail — otherwise A could complete work B is now doing.
    """
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    a = claim_next_step(conn, step_name="fetch")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_step_attempt "
            "SET lease_expires_at = now() - interval '1 minute' "
            "WHERE id = %s",
            (str(a.attempt_id),),
        )
    conn.commit()
    b = claim_next_step(conn, step_name="fetch")
    conn.commit()
    assert b.lease_token != a.lease_token

    # A's stale heartbeat must fail.
    assert heartbeat(conn, attempt_id=a.attempt_id,
                     lease_token=a.lease_token) is False
    # B's fresh heartbeat succeeds.
    assert heartbeat(conn, attempt_id=b.attempt_id,
                     lease_token=b.lease_token) is True
    conn.commit()


# ---------------------------------------------------------------------------
# 4. Retry classification
# ---------------------------------------------------------------------------

def test_transient_error_under_limit_enqueues_fresh_attempt(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    run_id = create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    claimed = claim_next_step(conn, step_name="fetch")
    conn.commit()

    classify_and_record(
        conn,
        attempt_id=claimed.attempt_id,
        lease_token=claimed.lease_token,
        run_id=run_id,
        step_name="fetch",
        attempt_number=1,
        error_class=ErrorClass.TRANSIENT,
        error_detail={"why": "network flap"},
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_number, status FROM workflow_step_attempt "
            "WHERE workflow_run_id = %s ORDER BY attempt_number",
            (str(run_id),),
        )
        rows = cur.fetchall()
    assert rows == [(1, "failed"), (2, "pending")]

    # The next claim picks up attempt 2.
    next_claim = claim_next_step(conn, step_name="fetch")
    assert next_claim is not None
    assert next_claim.attempt_number == 2


def test_transient_at_limit_dead_letters(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    run_id = create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    claimed = claim_next_step(conn, step_name="fetch")
    conn.commit()

    classify_and_record(
        conn,
        attempt_id=claimed.attempt_id,
        lease_token=claimed.lease_token,
        run_id=run_id,
        step_name="fetch",
        attempt_number=5,       # equal to MAX_TRANSIENT_ATTEMPTS default
        error_class=ErrorClass.TRANSIENT,
        error_detail={"why": "gave up"},
        replay_payload={"url": "https://example.com"},
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM workflow_step_attempt WHERE id = %s",
            (str(claimed.attempt_id),),
        )
        assert cur.fetchone()[0] == "dead_lettered"

        cur.execute("SELECT reason, replay_payload::text FROM dead_letter_item "
                    "WHERE workflow_step_attempt_id = %s",
                    (str(claimed.attempt_id),))
        row = cur.fetchone()
        assert row[0] == "transient_exhausted"
        assert "example.com" in row[1]


def test_input_error_dead_letters_immediately(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    run_id = create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    claimed = claim_next_step(conn, step_name="fetch")
    conn.commit()

    classify_and_record(
        conn,
        attempt_id=claimed.attempt_id,
        lease_token=claimed.lease_token,
        run_id=run_id,
        step_name="fetch",
        attempt_number=1,          # first try, but INPUT is terminal
        error_class=ErrorClass.INPUT,
        error_detail={"why": "malformed url"},
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM workflow_step_attempt WHERE id = %s",
                    (str(claimed.attempt_id),))
        assert cur.fetchone()[0] == "dead_lettered"

        cur.execute("SELECT reason FROM dead_letter_item "
                    "WHERE workflow_step_attempt_id = %s",
                    (str(claimed.attempt_id),))
        assert cur.fetchone()[0] == "input_failure"

        cur.execute("SELECT COUNT(*) FROM workflow_step_attempt "
                    "WHERE workflow_run_id = %s AND status = 'pending'",
                    (str(run_id),))
        assert cur.fetchone()[0] == 0, "no retry for INPUT failure"


def test_policy_error_dead_letters_for_human_review(conn):
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    run_id = create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    claimed = claim_next_step(conn, step_name="fetch")
    conn.commit()

    classify_and_record(
        conn,
        attempt_id=claimed.attempt_id,
        lease_token=claimed.lease_token,
        run_id=run_id,
        step_name="fetch",
        attempt_number=1,
        error_class=ErrorClass.POLICY,
        error_detail={"why": "unsigned dependency"},
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM dead_letter_item "
                    "WHERE workflow_step_attempt_id = %s",
                    (str(claimed.attempt_id),))
        assert cur.fetchone()[0] == "policy_failure"


# ---------------------------------------------------------------------------
# 5. Integration: worker dies mid-stage, run resumes without duplication
# ---------------------------------------------------------------------------

def test_worker_death_mid_stage_run_resumable_no_duplicate_side_effects(conn):
    """
    The Step 3 done-when, exactly:
      - Worker A claims a step, does the entity update + outbox in a
        transaction, then dies BEFORE marking succeeded and BEFORE the
        transaction commits.
      - Worker B's claim (after lease expiry) picks up the same row.
      - Worker B completes cleanly.
      - The final side effects (state transition, outbox event, entity
        update) exist exactly once.
    """
    asset_id = _make_provider_and_asset(conn)
    rev_id = _make_revision(conn, asset_id)
    run_id = create_run(
        conn, definition="test", definition_version=1,
        entity_kind="source_revision", entity_id=rev_id,
        initial_steps=["fetch"],
    )
    conn.commit()

    # Worker A claims and does the work, but the process dies before commit.
    conn_a = connect(for_tests=True)
    try:
        claim_a = claim_next_step(conn_a, step_name="fetch")
        assert claim_a is not None

        attempt_transition(
            conn_a,
            entity_kind="source_revision",
            entity_id=rev_id,
            from_state="identified",
            to_state="snapshot_queued",
            entity_update_sql=(
                "UPDATE source_revision SET snapshot_status = 'snapshot_queued' "
                "WHERE id = %s"
            ),
            entity_update_params=(str(rev_id),),
            events=[OutboxEvent(
                aggregate_kind="source_revision",
                aggregate_id=rev_id,
                event_type="revision.queued",
                payload={},
            )],
        )
        # Simulate crash: no commit, close the connection. Postgres rolls
        # back everything worker A did.
    finally:
        conn_a.close()

    # Verify from a third connection: state, transition, outbox all clean.
    with connect(for_tests=True) as verify:
        with verify.cursor() as cur:
            cur.execute("SELECT snapshot_status FROM source_revision WHERE id = %s",
                        (str(rev_id),))
            assert cur.fetchone()[0] == "identified"
            cur.execute("SELECT COUNT(*) FROM state_transition")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT COUNT(*) FROM outbox_event")
            assert cur.fetchone()[0] == 0

    # Expire the lease so worker B can claim it.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_step_attempt "
            "SET lease_expires_at = now() - interval '1 minute' "
            "WHERE workflow_run_id = %s AND step_name = 'fetch'",
            (str(run_id),),
        )
    conn.commit()

    # Worker B: claim, do the work, commit, mark succeeded.
    conn_b = connect(for_tests=True)
    try:
        claim_b = claim_next_step(conn_b, step_name="fetch")
        assert claim_b is not None
        assert claim_b.attempt_id == claim_a.attempt_id
        attempt_transition(
            conn_b,
            entity_kind="source_revision",
            entity_id=rev_id,
            from_state="identified",
            to_state="snapshot_queued",
            entity_update_sql=(
                "UPDATE source_revision SET snapshot_status = 'snapshot_queued' "
                "WHERE id = %s"
            ),
            entity_update_params=(str(rev_id),),
            events=[OutboxEvent(
                aggregate_kind="source_revision",
                aggregate_id=rev_id,
                event_type="revision.queued",
                payload={},
            )],
        )
        mark_succeeded(conn_b, attempt_id=claim_b.attempt_id,
                       lease_token=claim_b.lease_token)
        conn_b.commit()
    finally:
        conn_b.close()

    # Final check: exactly one transition, one outbox event, entity updated.
    with connect(for_tests=True) as verify:
        with verify.cursor() as cur:
            cur.execute("SELECT snapshot_status FROM source_revision WHERE id = %s",
                        (str(rev_id),))
            assert cur.fetchone()[0] == "snapshot_queued"

            cur.execute("SELECT COUNT(*) FROM state_transition")
            assert cur.fetchone()[0] == 1

            cur.execute("SELECT COUNT(*) FROM outbox_event "
                        "WHERE event_type = 'revision.queued'")
            assert cur.fetchone()[0] == 1

            cur.execute("SELECT status FROM workflow_step_attempt "
                        "WHERE id = %s", (str(claim_a.attempt_id),))
            assert cur.fetchone()[0] == "succeeded"
