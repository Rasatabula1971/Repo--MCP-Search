-- Migration 003 — Analysis and evidence tables
-- Per Build Workflow Step 6.
--
-- Three tables:
--   analysis_run          — one row per attempt to analyse a revision
--   analysis_stage_result — one row per extractor within a run
--   evidence_item         — the actual evidence (APPEND-ONLY)
--
-- "Done when: every evidence item resolves to a specific byte range or
--  manifest key in a pinned revision. An evidence item that can't be
--  located is not evidence."

-- ---------------------------------------------------------------------
-- analysis_run
-- One attempt at analysing a revision. Multiple runs are allowed —
-- each preserves its own evidence. This is deliberately not append-only
-- itself (status transitions from queued -> running -> completed/failed)
-- but the evidence it produces IS.
--
-- extractor_config_version is a knob for reproducibility: bumping it
-- signals "our extractors changed, previous runs' evidence may need
-- refreshing". The no-op short-circuit checks this.
-- ---------------------------------------------------------------------
CREATE TABLE analysis_run (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_revision_id       UUID        NOT NULL REFERENCES source_revision(id),
    extractor_config_version INTEGER     NOT NULL,
    status                   TEXT        NOT NULL DEFAULT 'queued',
                                            -- queued / running / completed / failed
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at               TIMESTAMPTZ,
    finished_at              TIMESTAMPTZ,
    metadata                 JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX analysis_run_revision_idx ON analysis_run(source_revision_id);
CREATE INDEX analysis_run_status_idx   ON analysis_run(status);

-- ---------------------------------------------------------------------
-- analysis_stage_result
-- One extractor's outcome within an analysis_run. Isolation: if one
-- extractor blows up, the others still complete — each has its own row.
-- ---------------------------------------------------------------------
CREATE TABLE analysis_stage_result (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_id   UUID        NOT NULL REFERENCES analysis_run(id)
                                    ON DELETE CASCADE,
    extractor_name    TEXT        NOT NULL,
    status            TEXT        NOT NULL,     -- succeeded / failed / skipped
    evidence_count    INTEGER     NOT NULL DEFAULT 0,
    error_class       TEXT,                       -- transient/input/policy
    error_detail      JSONB,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT analysis_stage_result_unique_per_run
        UNIQUE (analysis_run_id, extractor_name)
);

CREATE INDEX analysis_stage_result_run_idx ON analysis_stage_result(analysis_run_id);

-- ---------------------------------------------------------------------
-- evidence_item
-- The actual evidence. APPEND-ONLY — rules block UPDATE and DELETE.
--
-- locator_kind tells us how to interpret the locator JSON:
--   'file_range':  {"path": "src/x.py", "start_line": 10, "end_line": 20}
--   'manifest_key':{"path": "pyproject.toml", "key_path": ["project","dependencies","httpx"]}
--   'whole_file':  {"path": "LICENSE"}
--
-- extracted_value carries whatever the extractor observed. Deliberately
-- flexible (JSONB) — interpretation is Step 7+, not Step 6.
-- ---------------------------------------------------------------------
CREATE TABLE evidence_item (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_revision_id  UUID        NOT NULL REFERENCES source_revision(id),
    analysis_run_id     UUID        NOT NULL REFERENCES analysis_run(id),
    extractor_name      TEXT        NOT NULL,
    evidence_type       TEXT        NOT NULL,   -- 'dependency' | 'interface' |
                                                  -- 'test_indicator' | 'license' |
                                                  -- 'secret_indicator' | ...
    locator_kind        TEXT        NOT NULL,   -- see comment above
    locator             JSONB       NOT NULL,
    extracted_value     JSONB       NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT evidence_item_locator_kind_valid
        CHECK (locator_kind IN ('file_range', 'manifest_key', 'whole_file'))
);

CREATE INDEX evidence_item_revision_idx ON evidence_item(source_revision_id);
CREATE INDEX evidence_item_run_idx      ON evidence_item(analysis_run_id);
CREATE INDEX evidence_item_type_idx     ON evidence_item(evidence_type);

-- Append-only enforcement (same pattern as state_transition).
CREATE RULE evidence_item_no_update AS
    ON UPDATE TO evidence_item DO INSTEAD NOTHING;
CREATE RULE evidence_item_no_delete AS
    ON DELETE TO evidence_item DO INSTEAD NOTHING;
