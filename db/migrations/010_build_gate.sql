-- Migration 010 — Search-before-build gate
-- Per Build Workflow Step 13.
--
-- One table:
--   build_authorization — an audited record that a specific BUILD
--                          recommendation has been explicitly authorized
--                          for code generation. Append-only, one per
--                          recommendation, requires actor + reason.
--
-- The Step 13 done-when — "attempting to generate before evaluating
-- is refused by the workflow itself, not by convention" — is enforced
-- by core.policy.build_gate:
--   - can_generate_for(requirement_id) returns REFUSED unless a valid
--     BUILD recommendation exists AND has been authorized.
--   - authorize_build() refuses non-BUILD verdicts AND refuses BUILD
--     recommendations that lack search evidence (no candidate rows
--     AND rule_name != '__no_candidates__').
--
-- The table's role is the audit trail. The refusal is code-level.

CREATE TABLE build_authorization (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id UUID        NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
    authorized_by     TEXT        NOT NULL,
    reason            TEXT        NOT NULL,
    authorized_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT build_authorization_actor_not_empty
        CHECK (length(trim(authorized_by)) > 0),
    CONSTRAINT build_authorization_reason_not_empty
        CHECK (length(trim(reason)) > 0),
    CONSTRAINT build_authorization_unique_per_recommendation
        UNIQUE (recommendation_id)
);

CREATE INDEX build_authorization_recommendation_idx
    ON build_authorization(recommendation_id);

-- Append-only for UPDATE: an authorization's actor/reason cannot be
-- silently rewritten. DELETE, however, is INTENTIONALLY allowed via the
-- FK CASCADE — when the underlying recommendation is replaced by a
-- re-search, the authorization is meaningless and goes with it. That's
-- how "one authorization, one search" stays real. Adding an INSTEAD DO
-- NOTHING rule on DELETE would break the CASCADE and leave orphan rows.
CREATE RULE build_authorization_no_update AS
    ON UPDATE TO build_authorization DO INSTEAD NOTHING;
