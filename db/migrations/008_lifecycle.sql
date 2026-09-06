-- Migration 008 — Capability version lifecycle
-- Per Build Workflow Step 11.
--
-- Adds two columns to capability_version:
--   lifecycle_state — where in the pipeline this version is
--   summary          — human-readable description (populated by judgment
--                       later; the field exists here so guard 6 has
--                       something to check)
--
-- The state transitions themselves are audited in state_transition
-- (Migration 002). Lifecycle here is the current state; history lives
-- there. Same pattern as source_revision.snapshot_status vs.
-- state_transition rows.

ALTER TABLE capability_version
    ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'candidate',
    ADD COLUMN summary         TEXT;

ALTER TABLE capability_version
    ADD CONSTRAINT capability_version_lifecycle_valid
        CHECK (lifecycle_state IN (
            'candidate',
            'analyzed',
            'verified',
            'cataloged',
            'stale',
            'quarantined',
            'deprecated',
            'revoked'
        ));

CREATE INDEX capability_version_lifecycle_idx
    ON capability_version(lifecycle_state);

-- Fast query: "give me everything currently cataloged."
CREATE INDEX capability_version_cataloged_idx
    ON capability_version(capability_id)
    WHERE lifecycle_state = 'cataloged';
