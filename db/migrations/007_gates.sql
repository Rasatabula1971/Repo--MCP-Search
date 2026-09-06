-- Migration 007 — Policy gates
-- Per Build Workflow Step 10.
--
-- Two tables:
--   gate_result       — one row per (capability_version, gate) with a
--                        four-way status. Rewritten on re-evaluation.
--   risk_acceptance   — append-only overrides. A failed blocking gate
--                        that lacks a risk_acceptance row keeps the
--                        capability_version out of CATALOGED.
--
-- "A failed blocking gate stops publication and is never silently
--  overridden — an override is an audited risk_acceptance record."

-- ---------------------------------------------------------------------
-- gate_result
-- One row per gate per capability_version. Status is one of four
-- values, enforced by CHECK. is_blocking is a per-row property (not
-- global) because a gate's blocking-ness may depend on the profile
-- Step 15+ picks. For MVP it's set from the gate's declared default.
--
-- The unique constraint on (capability_version_id, gate_name) means
-- re-evaluation UPSERTs — we never keep stale rows around alongside
-- fresh ones. This table is a derived view; if you want history look
-- at state_transition.
-- ---------------------------------------------------------------------
CREATE TABLE gate_result (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    gate_name             TEXT        NOT NULL,
    status                TEXT        NOT NULL,
    is_blocking           BOOLEAN     NOT NULL,
    reason                TEXT,
    detail                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT gate_result_unique_per_version
        UNIQUE (capability_version_id, gate_name),
    CONSTRAINT gate_result_status_valid
        CHECK (status IN ('pass', 'warn', 'fail', 'not_evaluated'))
);

CREATE INDEX gate_result_capability_version_idx
    ON gate_result(capability_version_id);

-- Query: "does this capability_version have any unaccepted blocking
-- failures?" runs against this index.
CREATE INDEX gate_result_blocking_failure_idx
    ON gate_result(capability_version_id)
    WHERE status = 'fail' AND is_blocking = true;

-- ---------------------------------------------------------------------
-- risk_acceptance
-- APPEND-ONLY. A row here overrides one specific failed blocking gate
-- on one specific capability_version.
--
-- accepted_by and reason are both required and non-empty — the whole
-- point is that the override is auditable. An empty reason is not an
-- override, it's a silent bypass.
-- ---------------------------------------------------------------------
CREATE TABLE risk_acceptance (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    gate_result_id UUID        NOT NULL REFERENCES gate_result(id) ON DELETE CASCADE,
    accepted_by    TEXT        NOT NULL,
    reason         TEXT        NOT NULL,
    accepted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT risk_acceptance_reason_not_empty
        CHECK (length(trim(reason))   > 0),
    CONSTRAINT risk_acceptance_actor_not_empty
        CHECK (length(trim(accepted_by)) > 0)
);

CREATE INDEX risk_acceptance_gate_idx ON risk_acceptance(gate_result_id);

-- Append-only enforcement (same pattern as state_transition, evidence_item).
CREATE RULE risk_acceptance_no_update AS
    ON UPDATE TO risk_acceptance DO INSTEAD NOTHING;
CREATE RULE risk_acceptance_no_delete AS
    ON DELETE TO risk_acceptance DO INSTEAD NOTHING;
