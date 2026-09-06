-- Migration 005 — Capability registry
-- Per Build Workflow Step 8.
--
-- Five tables:
--   capability                  — abstract identity, keyed on normalized_key
--   capability_version          — a specific instantiation
--   capability_source_binding   — links a version to a source revision
--   capability_interface        — the surface exposed at a version
--   capability_dependency       — what a version depends on
--
-- Getting normalized_key right is the whole point of Step 8:
--   two repos implementing the same capability share a capability;
--   two commits of one repo produce two capability_version rows.
--
-- Interfaces and dependencies each carry an evidence_item_id — that's
-- what enforces "every capability attribute has a resolvable evidence
-- pointer" at query time.

-- ---------------------------------------------------------------------
-- capability
-- The abstract identity. normalized_key looks like:
--   pypi:requests            — published to PyPI as 'requests'
--   npm:react                — published to npm as 'react'
--   source:github:acme/foo   — has no published identity; keyed on source
--
-- The uniqueness constraint on normalized_key is what makes "two repos
-- publishing under the same name share a capability" real.
-- ---------------------------------------------------------------------
CREATE TABLE capability (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    normalized_key TEXT        NOT NULL UNIQUE,
    display_name   TEXT        NOT NULL,
    ecosystem      TEXT,                             -- 'pypi', 'npm', 'source', null
    kind           TEXT        NOT NULL,             -- 'library', 'cli', 'service', ...
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX capability_ecosystem_idx ON capability(ecosystem);

-- ---------------------------------------------------------------------
-- capability_version
-- A specific instantiation. version_key is content-derived by default
-- (content_hash prefix) so two commits with different bytes always
-- produce distinct versions, and re-analysing the same commit is
-- idempotent.
--
-- superseded_by_id is set only within a lineage: same capability, same
-- source_asset (i.e. same repo). Two competing implementations from
-- different repos don't auto-supersede each other — that's Step 11's
-- job to reason about.
-- ---------------------------------------------------------------------
CREATE TABLE capability_version (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id      UUID        NOT NULL REFERENCES capability(id) ON DELETE CASCADE,
    version_key        TEXT        NOT NULL,        -- e.g. 'content:abc12345'
    version_kind       TEXT        NOT NULL,        -- 'content-hash' | 'semver' | 'commit-derived'
    display_version    TEXT,                          -- '0.1.0' from manifest, if known
    superseded_by_id   UUID        REFERENCES capability_version(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT capability_version_unique
        UNIQUE (capability_id, version_key)
);

CREATE INDEX capability_version_capability_idx ON capability_version(capability_id);
CREATE INDEX capability_version_head_idx
    ON capability_version(capability_id)
    WHERE superseded_by_id IS NULL;

-- ---------------------------------------------------------------------
-- capability_source_binding
-- Links a version to the source_revision that produced it. Multiple
-- bindings per version are legal (path_scope != '' for monorepo carve-
-- outs). For MVP we only produce whole-repo bindings.
-- ---------------------------------------------------------------------
CREATE TABLE capability_source_binding (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    source_revision_id    UUID        NOT NULL REFERENCES source_revision(id),
    path_scope            TEXT        NOT NULL DEFAULT '',  -- '' = whole repo
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT capability_source_binding_unique
        UNIQUE (capability_version_id, source_revision_id, path_scope)
);

CREATE INDEX capability_source_binding_revision_idx
    ON capability_source_binding(source_revision_id);

-- ---------------------------------------------------------------------
-- capability_interface
-- One row per public function/class the capability exposes. evidence_
-- item_id points to the InterfaceExtractor row that discovered it —
-- delete-cascaded because if the evidence is gone, the derived row is
-- meaningless.
-- ---------------------------------------------------------------------
CREATE TABLE capability_interface (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    evidence_item_id      UUID        NOT NULL REFERENCES evidence_item(id),
    kind                  TEXT        NOT NULL,     -- 'function' | 'async_function' | 'class'
    name                  TEXT        NOT NULL,
    signature             TEXT,
    language              TEXT,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX capability_interface_version_idx
    ON capability_interface(capability_version_id);

-- ---------------------------------------------------------------------
-- capability_dependency
-- One row per declared dependency, sourced from ManifestExtractor
-- evidence. Same evidence_item_id link.
-- ---------------------------------------------------------------------
CREATE TABLE capability_dependency (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_version_id UUID        NOT NULL REFERENCES capability_version(id) ON DELETE CASCADE,
    evidence_item_id      UUID        NOT NULL REFERENCES evidence_item(id),
    depends_on_ecosystem  TEXT        NOT NULL,     -- 'pypi', 'npm'
    depends_on_name       TEXT        NOT NULL,
    version_spec          TEXT        NOT NULL DEFAULT '',
    dep_kind              TEXT        NOT NULL,     -- 'runtime' | 'dev' | 'peer' | 'optional:...'
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX capability_dependency_version_idx
    ON capability_dependency(capability_version_id);
CREATE INDEX capability_dependency_target_idx
    ON capability_dependency(depends_on_ecosystem, depends_on_name);
