-- Migration 012 — Ingest run tracking
-- Per CIP Foundation Phase 2.
--
-- Every ingester (github_search, awesome_list, mcp_registry, claude_skills,
-- gemini_enrich) records what it did on each run so:
--   - Next run can resume from the last cursor ("since X").
--   - Deprecation detection can compare current vs. previous scan.
--   - We have an audit trail of when/where data came from.
--
-- One table, append-only. Each successful run appends one row.

CREATE TABLE ingest_run (
    id                UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    source_name       TEXT          NOT NULL,        -- 'github_search', 'awesome_list:ad-si/awesome-video-production', 'mcp_registry', 'claude_skills'
    cursor            TEXT,                            -- ISO timestamp, page number, SHA, or NULL for full-scan runs
    started_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT          NOT NULL DEFAULT 'running',
                                                       -- 'running' | 'completed' | 'failed'
    counts            JSONB         NOT NULL DEFAULT '{}'::jsonb,
                                                       -- {new: N, updated: M, unchanged: K, deprecated: D, errors: E}
    error_detail      JSONB,
    metadata          JSONB         NOT NULL DEFAULT '{}'::jsonb,
                                                       -- query args, ratelimit remaining, etc.
    CONSTRAINT ingest_run_status_valid
        CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE INDEX ingest_run_source_started_idx
    ON ingest_run(source_name, started_at DESC);

-- Convenience view: the most recent COMPLETED run per source. Used by
-- ingesters to figure out "what was our cursor last time?"
CREATE VIEW ingest_run_latest AS
    SELECT DISTINCT ON (source_name)
        source_name, id, cursor, started_at, finished_at, counts, metadata
    FROM ingest_run
    WHERE status = 'completed'
    ORDER BY source_name, finished_at DESC NULLS LAST;
