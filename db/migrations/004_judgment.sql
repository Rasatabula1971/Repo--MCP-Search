-- Migration 004 — Judgment tables
-- Per Build Workflow Step 7.
--
-- Build the FULL shape even for one provider. Otherwise consensus at
-- Step 15 requires a history-rewriting migration.
--
-- Five tables:
--   model_provider    — one row per provider config (gemini, openai, ...)
--   model_profile     — one row per (provider, model_id) — model-level knobs
--   judgment_request  — one row per "we asked the model something"
--   judgment_response — one row per response received (valid OR malformed)
--   judgment_error    — one row per provider error (transport, rate limit)
--
-- Design notes:
--   - Provider identity is data, not code. No CHECK constraint listing
--     known providers. The registry loads them from providers.yaml at
--     boot; new providers require yaml edits + a row here, never code.
--   - judgment_response.schema_valid captures "did the model return
--     something that matched our schema?" — malformed rows are the
--     provider-reliability evidence the Step 7 done-when calls for.
--   - Every judgment_request records prompt_name + prompt_version +
--     prompt_hash. That triple is what makes "why did we get this
--     answer last month" answerable.

-- ---------------------------------------------------------------------
-- model_provider
-- One row per provider. api_key_env is the NAME of the env var, not the
-- key itself — secrets never land in the DB.
--
-- data_retention_posture is what OD-04 in the PDR cares about: does the
-- provider train on our input? A capability that includes proprietary
-- code can only be judged by providers with no_retention or better.
-- ---------------------------------------------------------------------
CREATE TABLE model_provider (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                     TEXT        NOT NULL UNIQUE,
    tier                     TEXT        NOT NULL,   -- 'free_primary' | 'free_secondary' | 'paid_escalation'
    base_url                 TEXT        NOT NULL,
    api_key_env              TEXT        NOT NULL,   -- name only, not the key
    data_retention_posture   TEXT        NOT NULL,   -- 'trains_on_input' | 'no_retention' | 'zdr'
    enabled                  BOOLEAN     NOT NULL DEFAULT true,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata                 JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------
-- model_profile
-- One row per model exposed by a provider. Tool-calling capability and
-- enable/disable knob live here.
-- ---------------------------------------------------------------------
CREATE TABLE model_profile (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id       UUID        NOT NULL REFERENCES model_provider(id) ON DELETE CASCADE,
    model_id          TEXT        NOT NULL,        -- e.g. 'gemini-2.0-flash'
    tool_calling      BOOLEAN     NOT NULL DEFAULT false,
    enabled           BOOLEAN     NOT NULL DEFAULT true,
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT model_profile_unique_per_provider
        UNIQUE (provider_id, model_id)
);

CREATE INDEX model_profile_provider_idx ON model_profile(provider_id);

-- ---------------------------------------------------------------------
-- judgment_request
-- "We asked something." The prompt_hash is the SHA256 of the rendered
-- prompt text — that plus prompt_name + prompt_version is enough to
-- reproduce the exact input on demand.
--
-- input_evidence_ids is the set of evidence_item rows the prompt was
-- built from. Auditable by design: "why did the model see X and not Y?"
-- has a definite answer.
-- ---------------------------------------------------------------------
CREATE TABLE judgment_request (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_revision_id    UUID        NOT NULL REFERENCES source_revision(id),
    analysis_run_id       UUID        REFERENCES analysis_run(id),
    prompt_name           TEXT        NOT NULL,
    prompt_version        INTEGER     NOT NULL,
    prompt_hash           TEXT        NOT NULL,   -- SHA256 of rendered prompt
    input_evidence_ids    UUID[]      NOT NULL DEFAULT ARRAY[]::UUID[],
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX judgment_request_revision_idx ON judgment_request(source_revision_id);
CREATE INDEX judgment_request_prompt_idx   ON judgment_request(prompt_name, prompt_version);

-- ---------------------------------------------------------------------
-- judgment_response
-- One row per response received. Rows exist even when the schema
-- didn't validate — those rows ARE the provider-reliability signal
-- Step 7's done-when talks about.
--
-- Parsed fields are nullable because a schema-invalid response has
-- nothing to parse; raw_response is NOT NULL so we can always audit
-- what was actually returned.
-- ---------------------------------------------------------------------
CREATE TABLE judgment_response (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    judgment_request_id   UUID        NOT NULL REFERENCES judgment_request(id) ON DELETE CASCADE,
    provider_id           UUID        NOT NULL REFERENCES model_provider(id),
    model_profile_id      UUID        NOT NULL REFERENCES model_profile(id),
    -- Parsed judgment fields — null when schema_valid = false.
    verdict               TEXT,
    criteria_scores       JSONB,
    self_confidence       NUMERIC(3,2),  -- 0.00 to 1.00
    evidence_refs         JSONB,          -- list of evidence_item ids
    -- Validation outcome + audit.
    schema_valid          BOOLEAN     NOT NULL,
    schema_error          TEXT,           -- populated when !schema_valid
    raw_response          TEXT        NOT NULL,
    -- Cost telemetry.
    latency_ms            INTEGER     NOT NULL,
    tokens_input          INTEGER,
    tokens_output         INTEGER,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX judgment_response_request_idx  ON judgment_response(judgment_request_id);
CREATE INDEX judgment_response_provider_idx ON judgment_response(provider_id);
-- Fast reliability queries: "how often does provider X return valid
-- responses at prompt version V?"
CREATE INDEX judgment_response_valid_idx    ON judgment_response(provider_id, schema_valid);

-- ---------------------------------------------------------------------
-- judgment_error
-- Provider-side errors that PREVENTED a response (transport, timeout,
-- rate limit, auth). Different from schema-invalid responses which
-- succeeded at the transport level but failed to parse.
-- ---------------------------------------------------------------------
CREATE TABLE judgment_error (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    judgment_request_id   UUID        NOT NULL REFERENCES judgment_request(id) ON DELETE CASCADE,
    provider_id           UUID        REFERENCES model_provider(id),
    error_class           TEXT        NOT NULL,   -- 'transient' | 'input' | 'policy'
    error_detail          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX judgment_error_request_idx ON judgment_error(judgment_request_id);
