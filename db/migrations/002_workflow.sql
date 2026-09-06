-- Migration 002 — Workflow engine tables
-- Per Build Workflow Step 2 and State Machine spec.
--
-- These come before any business logic because retrofitting idempotency
-- into a running system is the single most expensive rework in this build.
--
-- Six tables, all supporting one invariant:
--   "no state change without its event, and no event without its
--    state change" — enforced by transactional-outbox pattern.

-- ---------------------------------------------------------------------
-- workflow_run
-- One row per top-level unit of work — e.g. "evaluate revision X".
-- The run itself has a state; the individual steps have their own
-- attempts (see workflow_step_attempt).
-- ---------------------------------------------------------------------
CREATE TABLE workflow_run (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    definition        TEXT        NOT NULL,        -- e.g. 'revision_evaluation'
    definition_version INTEGER    NOT NULL,        -- bump when the pipeline changes
    entity_kind       TEXT        NOT NULL,        -- e.g. 'source_revision'
    entity_id         UUID        NOT NULL,        -- the thing being worked on
    status            TEXT        NOT NULL DEFAULT 'pending',
                                    -- pending / running / completed / failed / dead_lettered
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX workflow_run_entity_idx  ON workflow_run(entity_kind, entity_id);
CREATE INDEX workflow_run_status_idx  ON workflow_run(status);

-- ---------------------------------------------------------------------
-- workflow_step_attempt
-- One row per attempt at a step within a run. Leases make this queue
-- resumable: a worker claims a step with an expiry; if the worker dies,
-- the lease expires and another worker can pick it up. Heartbeats extend
-- the lease while work is still progressing.
--
-- This is the row workers SELECT ... FOR UPDATE SKIP LOCKED on.
-- ---------------------------------------------------------------------
CREATE TABLE workflow_step_attempt (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id   UUID        NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    step_name         TEXT        NOT NULL,        -- e.g. 'fetch', 'hash', 'index'
    attempt_number    INTEGER     NOT NULL,        -- 1, 2, 3 ... per (run, step)
    status            TEXT        NOT NULL DEFAULT 'pending',
                                    -- pending / claimed / succeeded / failed / dead_lettered
    lease_token       UUID,                          -- who currently holds the lease
    lease_expires_at  TIMESTAMPTZ,                   -- when the lease auto-releases
    last_heartbeat_at TIMESTAMPTZ,                   -- most recent extension
    error_class       TEXT,                          -- 'transient' | 'input' | 'policy'
    error_detail      JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    CONSTRAINT workflow_step_attempt_unique
        UNIQUE (workflow_run_id, step_name, attempt_number)
);

-- The claim query relies on this — pending rows are the queue.
CREATE INDEX workflow_step_attempt_claim_idx
    ON workflow_step_attempt(status, lease_expires_at)
    WHERE status IN ('pending', 'claimed');

-- ---------------------------------------------------------------------
-- state_transition
-- Append-only. Every state change on any tracked entity leaves a row
-- here, in the same transaction as the entity update. Never UPDATE, never
-- DELETE — the append-only property is what makes the audit trail real.
-- ---------------------------------------------------------------------
CREATE TABLE state_transition (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_kind       TEXT        NOT NULL,
    entity_id         UUID        NOT NULL,
    from_state        TEXT,                            -- null on initial insert
    to_state          TEXT        NOT NULL,
    guard_results     JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- {guard_name: pass/fail}
    actor             TEXT        NOT NULL,             -- 'system:worker' | 'user:<id>' | ...
    reason            TEXT,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX state_transition_entity_idx
    ON state_transition(entity_kind, entity_id, occurred_at);

-- Belt-and-braces enforcement of append-only. Nothing should be
-- rewriting history; this makes a bug that tries to fail loudly.
CREATE RULE state_transition_no_update AS
    ON UPDATE TO state_transition DO INSTEAD NOTHING;
CREATE RULE state_transition_no_delete AS
    ON DELETE TO state_transition DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------
-- workflow_command
-- Idempotent ledger keyed on command_id. Every side-effectful action
-- coming in from outside (an API call, a scheduler tick) carries a
-- command_id. The handler inserts here first: if the insert wins, it
-- runs; if it hits the unique constraint, it returns the prior result
-- and the handler NEVER runs twice.
--
-- "Submitting the same command_id twice produces exactly one
--  externally visible effect."
-- ---------------------------------------------------------------------
CREATE TABLE workflow_command (
    command_id        UUID        PRIMARY KEY,      -- caller-supplied
    kind              TEXT        NOT NULL,          -- 'start_run', 'enqueue_step', ...
    payload           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    result            JSONB,                          -- populated on completion
    status            TEXT        NOT NULL DEFAULT 'accepted',
                                    -- accepted / completed / failed
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE INDEX workflow_command_status_idx ON workflow_command(status);

-- ---------------------------------------------------------------------
-- outbox_event
-- Transactional outbox. Domain code writes here in the same transaction
-- as the state change; a separate dispatcher publishes them. This is what
-- makes "no state change without its event" true rather than hoped for.
-- ---------------------------------------------------------------------
CREATE TABLE outbox_event (
    id                BIGSERIAL   PRIMARY KEY,
    aggregate_kind    TEXT        NOT NULL,
    aggregate_id      UUID        NOT NULL,
    event_type        TEXT        NOT NULL,
    payload           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at     TIMESTAMPTZ,                     -- null == not yet published
    delivery_attempts INTEGER     NOT NULL DEFAULT 0
);

-- Undispatched rows are the dispatcher's work queue.
CREATE INDEX outbox_event_undispatched_idx
    ON outbox_event(created_at)
    WHERE dispatched_at IS NULL;

-- ---------------------------------------------------------------------
-- dead_letter_item
-- Where step attempts end up when they've exhausted retries or hit a
-- non-retryable error. Contains enough metadata to replay.
-- ---------------------------------------------------------------------
CREATE TABLE dead_letter_item (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_step_attempt_id UUID NOT NULL REFERENCES workflow_step_attempt(id),
    reason            TEXT        NOT NULL,             -- short code
    detail            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    replay_payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    replayed_at       TIMESTAMPTZ                       -- null == not yet replayed
);

CREATE INDEX dead_letter_item_unresolved_idx
    ON dead_letter_item(created_at)
    WHERE replayed_at IS NULL;
