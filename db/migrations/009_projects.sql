-- Migration 009 — Projects and recommendations
-- Per Build Workflow Step 12.
--
-- Nine tables (eight from the brief + a rules profile table parallel
-- to scoring_profile):
--   recommendation_rules_profile — decision rules AS DATA, versioned
--   project                       — a project that needs capabilities
--   project_requirement           — one requirement per row
--   requirement_constraint        — constraints on a requirement
--   fit_evaluation                — per (requirement, capability_version)
--   fit_gap                       — specific mismatches, blocking or not
--   fit_evidence_link             — every fit claim → evidence
--   recommendation                — one per requirement — the verdict
--   recommendation_candidate      — every candidate considered, ranked
--
-- Rules baked in:
--   - Fit score lives on fit_evaluation. Intrinsic score lives on
--     scorecard. They are NEVER merged into a single number.
--   - Every recommendation names an exact revision to pin.
--   - Every material claim links back to evidence via fit_evidence_link
--     or (transitively) via capability_interface.evidence_item_id and
--     score_evidence_link.

-- ---------------------------------------------------------------------
-- recommendation_rules_profile
-- Parallel shape to scoring_profile: rules are declarative data,
-- immutable once (name, version) is registered.
-- ---------------------------------------------------------------------
CREATE TABLE recommendation_rules_profile (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    version      INTEGER     NOT NULL,
    profile_hash TEXT        NOT NULL,
    rules        JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recommendation_rules_profile_unique
        UNIQUE (name, version)
);

-- ---------------------------------------------------------------------
-- project
-- ---------------------------------------------------------------------
CREATE TABLE project (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL UNIQUE,
    description  TEXT,
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- project_requirement
-- One requirement per row. slug is a stable, human-readable id within
-- the project ('http-client', 'test-framework'); external references
-- use it rather than the UUID.
-- ---------------------------------------------------------------------
CREATE TABLE project_requirement (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID        NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug         TEXT        NOT NULL,
    description  TEXT        NOT NULL,
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT project_requirement_slug_unique
        UNIQUE (project_id, slug)
);

-- ---------------------------------------------------------------------
-- requirement_constraint
-- Constraints on a requirement. `kind` says how to interpret detail:
--   'required_interface'  → {"name": "get", "signature_contains": "url"}
--   'license_allowlist'   → {"spdx_ids": ["MIT", "Apache-2.0"]}
--   'forbidden_dependency'→ {"ecosystem": "pypi", "name": "leftpad"}
-- Adding a kind means adding a matcher in core.project.fit.
-- ---------------------------------------------------------------------
CREATE TABLE requirement_constraint (
    id                       UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id   UUID  NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    kind                     TEXT  NOT NULL,
    detail                   JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX requirement_constraint_req_idx
    ON requirement_constraint(project_requirement_id);

-- ---------------------------------------------------------------------
-- fit_evaluation
-- One row per (requirement, capability_version). fit_score is COMPUTED
-- from constraints and evidence — never blended with the capability's
-- intrinsic scorecard.
--
-- computed_hash lets us verify determinism the same way scorecard does.
-- ---------------------------------------------------------------------
CREATE TABLE fit_evaluation (
    id                       UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id   UUID          NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    capability_version_id    UUID          NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    fit_score                NUMERIC(4,3)  NOT NULL,     -- 0..1
    blocking_gap_count       INTEGER       NOT NULL,
    computed_hash            TEXT          NOT NULL,
    computed_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT fit_evaluation_unique
        UNIQUE (project_requirement_id, capability_version_id)
);

CREATE INDEX fit_evaluation_req_idx  ON fit_evaluation(project_requirement_id);
CREATE INDEX fit_evaluation_cv_idx   ON fit_evaluation(capability_version_id);

-- ---------------------------------------------------------------------
-- fit_gap
-- A specific mismatch between what the requirement asks for and what
-- the capability provides. `is_blocking` distinguishes "close enough"
-- from "cannot possibly work" — a blocking gap kicks the candidate out
-- of consideration for ADOPT/ADAPT, forcing REFERENCE/REJECT.
-- ---------------------------------------------------------------------
CREATE TABLE fit_gap (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    fit_evaluation_id UUID    NOT NULL REFERENCES fit_evaluation(id) ON DELETE CASCADE,
    kind              TEXT    NOT NULL,
    is_blocking       BOOLEAN NOT NULL,
    detail            JSONB   NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX fit_gap_eval_idx ON fit_gap(fit_evaluation_id);

-- ---------------------------------------------------------------------
-- fit_evidence_link
-- Which evidence_items support (or block) each fit_evaluation. This is
-- what makes "every material claim links to evidence" enforceable —
-- for any fit_evaluation you can list the evidence rows that fed into
-- it.
-- ---------------------------------------------------------------------
CREATE TABLE fit_evidence_link (
    id                UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
    fit_evaluation_id UUID  NOT NULL REFERENCES fit_evaluation(id) ON DELETE CASCADE,
    evidence_item_id  UUID  NOT NULL REFERENCES evidence_item(id),
    role              TEXT  NOT NULL,   -- 'supports_match'|'documents_gap'
    CONSTRAINT fit_evidence_link_unique
        UNIQUE (fit_evaluation_id, evidence_item_id, role)
);

CREATE INDEX fit_evidence_link_evidence_idx
    ON fit_evidence_link(evidence_item_id);

-- ---------------------------------------------------------------------
-- recommendation
-- One per requirement. verdict is one of six values, enforced by CHECK.
-- pinned_revision_key is the exact SHA (or provider-specific revision
-- identifier) the recommendation names. Empty for REJECT/BUILD.
-- ---------------------------------------------------------------------
CREATE TABLE recommendation (
    id                              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_requirement_id          UUID        NOT NULL REFERENCES project_requirement(id) ON DELETE CASCADE,
    rules_profile_id                UUID        NOT NULL REFERENCES recommendation_rules_profile(id),
    verdict                         TEXT        NOT NULL,
    chosen_capability_version_id    UUID        REFERENCES capability_version(id),
    pinned_revision_key             TEXT,       -- e.g. commit SHA
    pinned_source_asset_key         TEXT,       -- e.g. 'psf/requests'
    rule_name                       TEXT        NOT NULL,   -- which rule fired
    reason                          TEXT        NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recommendation_verdict_valid
        CHECK (verdict IN ('ADOPT', 'ADAPT', 'WRAP', 'REFERENCE', 'REJECT', 'BUILD')),
    -- Non-BUILD/REJECT verdicts MUST pin a revision. That's what makes
    -- "names the exact revision to pin" a schema-enforced invariant.
    CONSTRAINT recommendation_pin_required
        CHECK (
            verdict IN ('REJECT', 'BUILD')
            OR (chosen_capability_version_id IS NOT NULL
                AND pinned_revision_key IS NOT NULL
                AND pinned_source_asset_key IS NOT NULL)
        ),
    CONSTRAINT recommendation_unique_per_requirement
        UNIQUE (project_requirement_id)
);

CREATE INDEX recommendation_verdict_idx ON recommendation(verdict);

-- ---------------------------------------------------------------------
-- recommendation_candidate
-- Every candidate that was evaluated for a requirement, with its rank.
-- The winner is rank=1 and matches recommendation.chosen_capability_
-- version_id (when the verdict picks one).
-- ---------------------------------------------------------------------
CREATE TABLE recommendation_candidate (
    id                     UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id      UUID    NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
    capability_version_id  UUID    NOT NULL REFERENCES capability_version(id),
    fit_evaluation_id      UUID    NOT NULL REFERENCES fit_evaluation(id),
    rank                   INTEGER NOT NULL,
    CONSTRAINT recommendation_candidate_unique_cv
        UNIQUE (recommendation_id, capability_version_id),
    CONSTRAINT recommendation_candidate_unique_rank
        UNIQUE (recommendation_id, rank)
);

CREATE INDEX recommendation_candidate_rec_idx
    ON recommendation_candidate(recommendation_id);
