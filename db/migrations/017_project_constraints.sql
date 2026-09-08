-- Migration 017 — Project-level constraints (Phase 4)
--
-- Constraints that apply to EVERY component pick for a project, not
-- just to a single requirement. requirement_constraint (Step 12) is
-- per-requirement — "this feature needs an X interface". This table
-- is per-project — "any pick anywhere in this project must be free
-- tier, MIT-compatible, and must not require a GPU."
--
-- The pure evaluator in core.policy.constraints reads these + a
-- component's metadata and produces a fit verdict without touching
-- the DB itself.

CREATE TABLE project_constraint (
    id            UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID          NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    kind          TEXT          NOT NULL,
    detail        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT project_constraint_kind_valid
        CHECK (kind IN (
            'cpu_only',              -- forbids components that require GPU
            'no_gpu',                -- alias for cpu_only, same effect
            'must_be_free_tier',     -- cost_tier must be free or free_tier
            'must_be_local',         -- runtime must be locally executable (no http endpoint)
            'always_off_at_night',   -- forbids components that need to run continuously
            'budget_monthly_ceiling',-- detail: {amount_usd: 50}
            'license_allowlist',     -- detail: {spdx_ids: ["MIT","Apache-2.0"]}
            'license_denylist',      -- detail: {spdx_ids: ["AGPL-3.0"]}
            'runtime_allowlist',     -- detail: {runtimes: ["python_import","mcp_stdio"]}
            'component_kind_allowlist' -- detail: {kinds: ["library","repo"]}
        ))
);

CREATE INDEX project_constraint_project_idx
    ON project_constraint(project_id);
