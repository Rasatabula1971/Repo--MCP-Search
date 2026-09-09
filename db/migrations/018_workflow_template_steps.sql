-- Migration 018 — Workflow template steps (Phase 5a)
--
-- A workflow_template capability now gains real content: an ordered
-- list of steps, each pointing at a component + a role in the chain.
-- The template's OWN capability row keeps its display_name and
-- metadata; this table is where "here are the stages" lives.
--
-- Adapters are represented as capabilities too — a `workflow_template
-- _step` may reference a capability whose component_kind is 'library'
-- or 'repo' but whose semantic role in this template is 'adapter'
-- (glue between two other steps). The role is per-step, not per-
-- capability, because the same library can be a producer in one
-- template and an adapter in another.
--
-- Append-only? No — a template author edits steps repeatedly during
-- design. The template capability itself stays stable; step rows are
-- mutable per (template_id, step_index).

CREATE TABLE workflow_template_step (
    id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id           UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    step_index            INTEGER       NOT NULL,               -- 0-based order in the chain
    component_id          UUID          NOT NULL REFERENCES capability(id),
    role                  TEXT          NOT NULL,               -- 'producer' | 'transformer' | 'adapter' | 'sink' | 'gate'
    purpose               TEXT,                                    -- one-line what-this-step-does
    metadata              JSONB         NOT NULL DEFAULT '{}'::jsonb,
                                                                   -- {inputs_from: [step_indices], outputs_to: [step_indices], config: {...}}
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT workflow_template_step_role_valid
        CHECK (role IN ('producer', 'transformer', 'adapter', 'sink', 'gate')),
    CONSTRAINT workflow_template_step_unique_per_index
        UNIQUE (template_id, step_index)
);

CREATE INDEX workflow_template_step_template_idx
    ON workflow_template_step(template_id, step_index);
CREATE INDEX workflow_template_step_component_idx
    ON workflow_template_step(component_id);
