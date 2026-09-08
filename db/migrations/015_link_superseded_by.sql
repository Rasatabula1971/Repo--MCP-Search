-- Migration 015 — Extend capability_link.link_kind with 'superseded_by'
--
-- Phase 3c adds cross-capability supersession — the "this repo says use
-- FooV2 instead" signal LLM readers catch that source-derived rules
-- can't. Existing capability_version.superseded_by_id handles supersession
-- WITHIN a lineage (same repo, later commit); this covers supersession
-- ACROSS lineages (repo A deprecates itself, points at repo B).

ALTER TABLE capability_link
    DROP CONSTRAINT capability_link_kind_valid;

ALTER TABLE capability_link
    ADD CONSTRAINT capability_link_kind_valid
        CHECK (link_kind IN (
            'same_component', 'alternative_to', 'part_of',
            'depends_on_symbolic', 'superseded_by'
        ));
