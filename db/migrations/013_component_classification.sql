-- Migration 013 — Component classification (Phase 3a)
--
-- Records every classification decision an LLM makes about a component:
-- what kind is it (format), what role does it play (semantic), what does
-- it do (one-line purpose), how confident is the model, and why.
--
-- Append-only. A capability may accumulate multiple classifications
-- across time (prompt version bumps, model changes, README edits).
-- The `applied` flag captures whether we actually mutated the
-- capability row on the strength of this classification — a high-
-- confidence answer can update the row; a low-confidence answer is
-- kept as evidence but not acted on.
--
-- Provenance triple: prompt_name + prompt_version + prompt_hash is
-- what makes "why did we classify X this way last month" answerable.

CREATE TABLE component_classification (
    id                       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id            UUID          NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    -- What the model returned
    proposed_component_kind  TEXT          NOT NULL,
    proposed_capability_kind TEXT          NOT NULL,
    proposed_purpose         TEXT,
    confidence               NUMERIC(4,3)  NOT NULL,       -- 0.000 .. 1.000
    rationale                TEXT,
    -- What we did about it
    applied                  BOOLEAN       NOT NULL DEFAULT FALSE,
    apply_reason             TEXT,                          -- 'high_confidence' / 'suppressed_low_confidence' / 'no_change'
    prior_component_kind     TEXT,                          -- snapshot before this classification, for audit
    prior_capability_kind    TEXT,
    -- Provenance
    prompt_name              TEXT          NOT NULL,
    prompt_version           INTEGER       NOT NULL,
    prompt_hash              TEXT          NOT NULL,
    model_name               TEXT          NOT NULL,
    -- Timing
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX component_classification_capability_idx
    ON component_classification(capability_id, created_at DESC);
CREATE INDEX component_classification_prompt_idx
    ON component_classification(prompt_name, prompt_version);

-- Convenience view: the most recent classification per capability.
-- Used by the worker to decide "have we already classified this at the
-- current prompt version?"
CREATE VIEW component_classification_latest AS
    SELECT DISTINCT ON (capability_id)
        capability_id, id, proposed_component_kind, proposed_capability_kind,
        proposed_purpose, confidence, applied, prompt_name, prompt_version,
        model_name, created_at
    FROM component_classification
    ORDER BY capability_id, created_at DESC;
