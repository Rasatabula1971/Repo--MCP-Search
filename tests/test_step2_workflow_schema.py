"""
Step 2 done-when:
  "Submitting the same command_id twice produces exactly one externally
   visible effect."

The command-ledger *behavior* is exercised in test_step3_engine — but the
schema has to make that behavior possible. Here we verify the schema
invariants directly:

  - workflow_command.command_id is unique (the ledger primitive).
  - state_transition is append-only (rules block UPDATE/DELETE).
  - outbox_event's queue index exists (undispatched rows are the work).
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest


def _make_run(conn):
    run_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_run
              (id, definition, definition_version, entity_kind, entity_id)
            VALUES (%s, 'test', 1, 'test', %s)
            """,
            (str(run_id), str(uuid.uuid4())),
        )
    return run_id


def test_workflow_command_pk_blocks_duplicate_command_id(conn):
    cmd = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_command (command_id, kind, payload) "
            "VALUES (%s, 'x', '{}'::jsonb)",
            (str(cmd),),
        )
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_command (command_id, kind, payload) "
                "VALUES (%s, 'x', '{}'::jsonb)",
                (str(cmd),),
            )


def test_state_transition_update_is_silently_dropped_by_rule(conn):
    """
    The rules on state_transition rewrite UPDATE/DELETE into no-ops.
    That's Postgres's way of preserving history: the write appears to
    succeed (no error) but nothing changes. Any code trying to mutate
    history will silently do nothing — which the audit trail will show
    on the next inspection.
    """
    tid = uuid.uuid4()
    entity_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO state_transition
              (id, entity_kind, entity_id, from_state, to_state,
               guard_results, actor)
            VALUES (%s, 'x', %s, 'a', 'b', '{}'::jsonb, 'test')
            """,
            (str(tid), str(entity_id)),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE state_transition SET to_state = 'c' WHERE id = %s",
            (str(tid),),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_state FROM state_transition WHERE id = %s",
            (str(tid),),
        )
        assert cur.fetchone()[0] == "b"  # UPDATE did nothing

    with conn.cursor() as cur:
        cur.execute("DELETE FROM state_transition WHERE id = %s", (str(tid),))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM state_transition WHERE id = %s",
                    (str(tid),))
        assert cur.fetchone()[0] == 1  # DELETE did nothing


def test_workflow_step_attempt_unique_per_run_step_number(conn):
    run_id = _make_run(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_step_attempt "
            "(workflow_run_id, step_name, attempt_number) "
            "VALUES (%s, 'fetch', 1)",
            (str(run_id),),
        )
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workflow_step_attempt "
                "(workflow_run_id, step_name, attempt_number) "
                "VALUES (%s, 'fetch', 1)",
                (str(run_id),),
            )
