-- Migration 011 — Component kinds, runtime, cost, license, I/O types
-- Per CIP Foundation Phase 1.
--
-- Extends the capability model so it can represent any composable
-- software component, not only published packages. Adds:
--
--   capability.component_kind — what kind of thing this is
--     library            an importable package (pypi, npm, …)  ← existing default
--     repo               a whole GitHub project meant to be forked, run, or referenced
--     agent              a delegatable actor (Claude Code agent, LangChain agent)
--     skill              an instruction set that shapes an LLM (Claude skill, custom)
--     mcp_tool           a callable capability exposed via MCP
--     workflow_template  a reusable chain / recipe of other components
--
--   capability.runtime — how you invoke it, if applicable
--     Free-form text on purpose; we don't want to fight the schema every
--     time a new runtime shows up. Suggested vocabulary:
--       python_import, npm_import, cargo_import, go_import,
--       http_endpoint, mcp_stdio, mcp_sse, claude_skill, claude_agent,
--       cli_subprocess, git_clone, other
--
--   capability.cost_tier — coarse budget bucket, nullable when unknown
--     free, free_tier, cheap_paid, paid
--
--   capability.license_spdx — SPDX identifier of the primary license,
--     nullable when unknown. Held separately from evidence_item license
--     rows because it's the fast-path answer for gate checks; evidence
--     still lives in capability_dependency's license evidence.
--
--   capability_interface.input_type, .output_type — JSONB descriptors of
--     what an interface accepts and produces. NULL for now on every row;
--     Phase 3 (judgment) and Phase 5 (composition) will fill them.
--     JSONB rather than TEXT so structured shapes ({kind: 'object',
--     schema: {...}}) can land later without another migration.
--
-- Existing rows are backfilled with component_kind='library' so nothing
-- else in the code has to care about the new column being present.

-- ---------------------------------------------------------------------
-- capability new columns
-- ---------------------------------------------------------------------

ALTER TABLE capability
    ADD COLUMN component_kind TEXT NOT NULL DEFAULT 'library';

ALTER TABLE capability
    ADD CONSTRAINT capability_component_kind_valid
        CHECK (component_kind IN (
            'library', 'repo', 'agent', 'skill', 'mcp_tool', 'workflow_template'
        ));

ALTER TABLE capability
    ADD COLUMN runtime TEXT;

ALTER TABLE capability
    ADD COLUMN cost_tier TEXT;

ALTER TABLE capability
    ADD CONSTRAINT capability_cost_tier_valid
        CHECK (cost_tier IS NULL OR cost_tier IN (
            'free', 'free_tier', 'cheap_paid', 'paid'
        ));

ALTER TABLE capability
    ADD COLUMN license_spdx TEXT;

CREATE INDEX capability_component_kind_idx ON capability(component_kind);
CREATE INDEX capability_cost_tier_idx      ON capability(cost_tier)
    WHERE cost_tier IS NOT NULL;

-- ---------------------------------------------------------------------
-- capability_interface new columns
-- ---------------------------------------------------------------------

ALTER TABLE capability_interface
    ADD COLUMN input_type  JSONB;

ALTER TABLE capability_interface
    ADD COLUMN output_type JSONB;
