-- Migration 006 — Scoring
-- Per Build Workflow Step 9.
--
-- Four tables:
--   scoring_profile          — the rubric as data (dimensions + weights)
--   scorecard                — the top-level result for (cap_version, profile)
--   score_dimension_result   — one row per dimension in that scorecard
--   score_evidence_link      — which evidence contributed to which dimension
--
-- Rules baked in:
--   - Same evidence set + same profile → identical scorecard, byte for
--     byte. computed_hash is what makes 'byte for byte' verifiable.
--   - Confidence comes from evidence coverage + contradiction. Never
--     from judgment_response.self_confidence.
--   - Profile is data, not code. Bumping the version is what invalidates
--     prior scorecards.

-- ---------------------------------------------------------------------
-- scoring_profile
-- The rubric. Name + version is the natural key; profile_hash catches
-- accidental edits. Once a version is registered, its dimensions are
-- immutable — edits require a version bump.
-- ---------------------------------------------------------------------
CREATE TABLE scoring_profile (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT        NOT NULL,
    version       INTEGER     NOT NULL,
    profile_hash  TEXT        NOT NULL,          -- SHA256 of canonical yaml
    dimensions    JSONB       NOT NULL,          -- the actual dimension definitions
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scoring_profile_unique_per_version
        UNIQUE (name, version)
);

-- ---------------------------------------------------------------------
-- scorecard
-- Top-level scorecard for (capability_version, profile). One per pair.
-- computed_hash is a SHA256 over the sorted evidence ids + profile_hash;
-- a re-score that produces a different hash for the same inputs is a
-- determinism failure and blocks publication.
-- ---------------------------------------------------------------------
CREATE TABLE scorecard (
    id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID          NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    scoring_profile_id    UUID          NOT NULL REFERENCES scoring_profile(id),
    total_score           NUMERIC(4,3)  NOT NULL,        -- 0.000..1.000
    confidence            NUMERIC(4,3)  NOT NULL,        -- 0.000..1.000
    computed_hash         TEXT          NOT NULL,
    computed_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT scorecard_unique_per_profile
        UNIQUE (capability_version_id, scoring_profile_id)
);

CREATE INDEX scorecard_capability_version_idx
    ON scorecard(capability_version_id);

-- ---------------------------------------------------------------------
-- score_dimension_result
-- One row per dimension in the scorecard. Explicit rather than implicit —
-- lets us answer "why is confidence low?" by joining and inspecting.
-- ---------------------------------------------------------------------
CREATE TABLE score_dimension_result (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    scorecard_id    UUID          NOT NULL REFERENCES scorecard(id) ON DELETE CASCADE,
    dimension_name  TEXT          NOT NULL,
    raw_score       NUMERIC(4,3)  NOT NULL,     -- pre-weight, 0..1
    weight          NUMERIC(4,3)  NOT NULL,     -- from profile
    weighted_score  NUMERIC(5,4)  NOT NULL,     -- raw * weight
    coverage        NUMERIC(4,3)  NOT NULL,     -- 1.0 iff any evidence matched
    contradiction   NUMERIC(4,3)  NOT NULL,     -- 0.0 == consistent
    evidence_count  INTEGER       NOT NULL,
    metadata        JSONB         NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT score_dimension_result_unique
        UNIQUE (scorecard_id, dimension_name)
);

-- ---------------------------------------------------------------------
-- score_evidence_link
-- Which evidence items contributed to which dimension. This is what
-- makes the "evidence before claims" rule inspectable end-to-end: pick
-- any dimension result, follow the links, look at the source lines.
-- ---------------------------------------------------------------------
CREATE TABLE score_evidence_link (
    id                          UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
    score_dimension_result_id   UUID  NOT NULL REFERENCES score_dimension_result(id) ON DELETE CASCADE,
    evidence_item_id            UUID  NOT NULL REFERENCES evidence_item(id),
    CONSTRAINT score_evidence_link_unique
        UNIQUE (score_dimension_result_id, evidence_item_id)
);

CREATE INDEX score_evidence_link_evidence_idx
    ON score_evidence_link(evidence_item_id);
