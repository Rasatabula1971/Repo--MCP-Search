-- Migration 014 — Capability links (Phase 3b)
--
-- A directed link from one capability to another, with a typed
-- relationship. The 'same_component' link is what turns "a pypi
-- package and a github repo the model believes are the same thing"
-- into a first-class fact composition can rely on.
--
-- Link kinds:
--   same_component     — A and B are two facets of one real thing
--                        (e.g. pypi:requests and source:github:psf/requests)
--   alternative_to     — A is a substitute for B (same interface,
--                        different implementation)
--   part_of            — A is a subcomponent of B (a subdirectory
--                        skill, a monorepo package)
--   depends_on_symbolic - A conceptually depends on B (weaker than
--                        capability_dependency, which is source-derived)
--
-- We store links as directed (source -> target) but for 'same_component'
-- the semantics are symmetric — a query helper resolves both directions.
--
-- Append-only for a given (source, target, link_kind) — same triple
-- means same link. Bumping prompt_version and re-running creates a new
-- link row only if the classification changed.

CREATE TABLE capability_link (
    id                       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    source_capability_id     UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    target_capability_id     UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    link_kind                TEXT          NOT NULL,
    confidence               NUMERIC(4,3)  NOT NULL,
    rationale                TEXT,
    -- Provenance
    prompt_name              TEXT          NOT NULL,
    prompt_version           INTEGER       NOT NULL,
    prompt_hash              TEXT          NOT NULL,
    model_name               TEXT          NOT NULL,
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT capability_link_kind_valid
        CHECK (link_kind IN (
            'same_component', 'alternative_to', 'part_of', 'depends_on_symbolic'
        )),
    CONSTRAINT capability_link_no_self_link
        CHECK (source_capability_id <> target_capability_id),
    CONSTRAINT capability_link_unique_per_prompt
        UNIQUE (source_capability_id, target_capability_id, link_kind,
                prompt_name, prompt_version)
);

CREATE INDEX capability_link_source_idx ON capability_link(source_capability_id, link_kind);
CREATE INDEX capability_link_target_idx ON capability_link(target_capability_id, link_kind);

-- Same-component pairs (both directions). Used by the runner to know
-- "have we already asked about this pair?" without caring which side is source.
CREATE VIEW capability_link_same_component AS
    SELECT source_capability_id AS a_id, target_capability_id AS b_id,
           confidence, rationale, prompt_name, prompt_version, created_at
    FROM capability_link WHERE link_kind = 'same_component'
    UNION ALL
    SELECT target_capability_id AS a_id, source_capability_id AS b_id,
           confidence, rationale, prompt_name, prompt_version, created_at
    FROM capability_link WHERE link_kind = 'same_component';
