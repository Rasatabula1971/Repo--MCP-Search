-- Migration 016 — Per-kind component scoring (Phase 3d)
--
-- Registry-side scoring: signals derived from metadata alone (stars,
-- age, license clarity, description completeness), NOT from source-
-- code evidence. That kind of scoring already lives in Step 9's
-- scoring_profile / scorecard machinery — this table is deliberately
-- separate because:
--   1. Not every capability has a capability_version (many skills,
--      mcp_tools). scorecard requires one.
--   2. Metadata scoring is deterministic + cheap. No profile YAML,
--      no evidence linkage, just a formula per kind. Overkill to run
--      it through the full scoring engine.
--   3. Bumping a profile_version here doesn't invalidate Step 9
--      scores. The two systems stay decoupled.
--
-- Profile names match component_kind: 'repo_health', 'library_health',
-- 'skill_health', 'mcp_tool_health', 'agent_health',
-- 'workflow_template_health'. New kinds add new profiles.

CREATE TABLE component_score (
    id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id     UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    profile_name      TEXT          NOT NULL,
    profile_version   INTEGER       NOT NULL,
    total_score       NUMERIC(4,3)  NOT NULL,           -- 0.000 .. 1.000
    confidence        NUMERIC(4,3)  NOT NULL,           -- 0.000 .. 1.000
    dimensions        JSONB         NOT NULL DEFAULT '{}'::jsonb,
                                                          -- {dim_name: {raw, weight, weighted}}
    computed_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT component_score_unique_per_version
        UNIQUE (capability_id, profile_name, profile_version)
);

CREATE INDEX component_score_capability_idx
    ON component_score(capability_id);
CREATE INDEX component_score_profile_total_idx
    ON component_score(profile_name, total_score DESC);
