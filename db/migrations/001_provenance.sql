-- Migration 001 — Provenance chain
-- Per Build Workflow Step 1 and Data Model spec.
--
-- Order matters: each table depends on the previous one.
-- source_provider → source_asset → source_revision → file_artifact
--
-- Uniqueness rules that make ingestion idempotent:
--   source_asset:    (provider_id, external_key)
--   source_revision: (source_asset_id, revision_key)
--
-- "Done when: ingesting the same repo twice produces one source_asset
--  row and one source_revision row per distinct commit."

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

-- ---------------------------------------------------------------------
-- source_provider
-- One row per ecosystem CIP knows how to talk to (github, mcp, ...).
-- Name is the natural key; the row is created by the connector at boot.
-- ---------------------------------------------------------------------
CREATE TABLE source_provider (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT        NOT NULL UNIQUE,
    kind              TEXT        NOT NULL,              -- 'code_host', 'mcp_server_registry', ...
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- source_asset
-- A thing at a provider. For GitHub, this is a repository. external_key
-- is whatever the provider uses to identify it uniquely (e.g. 'owner/repo'
-- for GitHub). Uniqueness on (provider_id, external_key) is what makes
-- "ingest the same repo twice" a single row.
--
-- lifecycle_status is separate from any per-revision workflow state. That
-- separation is deliberate — the State Machine spec keeps lifecycle on the
-- asset and execution state on the revision so they can't tangle.
-- ---------------------------------------------------------------------
CREATE TABLE source_asset (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id       UUID        NOT NULL REFERENCES source_provider(id),
    external_key      TEXT        NOT NULL,             -- e.g. 'psf/requests'
    display_name      TEXT        NOT NULL,
    kind              TEXT        NOT NULL,             -- 'repository', 'mcp_server', 'skill'
    lifecycle_status  TEXT        NOT NULL DEFAULT 'active',  -- active / archived / blocked
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT source_asset_unique_per_provider
        UNIQUE (provider_id, external_key)
);

CREATE INDEX source_asset_provider_idx ON source_asset(provider_id);
CREATE INDEX source_asset_lifecycle_idx ON source_asset(lifecycle_status);

-- ---------------------------------------------------------------------
-- source_revision
-- A pinned point in time on an asset. For GitHub, revision_key is a
-- commit SHA. Uniqueness on (source_asset_id, revision_key) is what
-- makes re-ingesting the same commit a no-op.
--
-- content_hash is populated by Step 5 (ingestion). It's what lets
-- re-analysis skip work: same content_hash means the same bytes, means
-- prior evidence still applies.
--
-- snapshot_status tracks per-revision execution state, not lifecycle.
-- ---------------------------------------------------------------------
CREATE TABLE source_revision (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_asset_id   UUID        NOT NULL REFERENCES source_asset(id),
    revision_key      TEXT        NOT NULL,             -- e.g. commit SHA
    content_hash      TEXT,                              -- filled at hashing stage
    snapshot_status   TEXT        NOT NULL DEFAULT 'identified',
                                    -- identified / snapshot_queued / fetching / hashing /
                                    -- indexing / snapshotted / unavailable / blocked
    identified_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshotted_at    TIMESTAMPTZ,
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT source_revision_unique_per_asset
        UNIQUE (source_asset_id, revision_key)
);

CREATE INDEX source_revision_asset_idx ON source_revision(source_asset_id);
CREATE INDEX source_revision_status_idx ON source_revision(snapshot_status);

-- ---------------------------------------------------------------------
-- file_artifact
-- One row per file in a revision's snapshot. We store path/size/language/
-- hash — NOT content, unless a later storage decision changes that.
-- ---------------------------------------------------------------------
CREATE TABLE file_artifact (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_revision_id UUID       NOT NULL REFERENCES source_revision(id) ON DELETE CASCADE,
    path              TEXT        NOT NULL,
    size_bytes        BIGINT      NOT NULL,
    language          TEXT,
    content_hash      TEXT        NOT NULL,
    CONSTRAINT file_artifact_unique_per_revision
        UNIQUE (source_revision_id, path)
);

CREATE INDEX file_artifact_revision_idx ON file_artifact(source_revision_id);
CREATE INDEX file_artifact_content_hash_idx ON file_artifact(content_hash);
