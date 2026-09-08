"""
Phase 1 done-when for the composable-component foundation:

  "search_capabilities and capability_detail expose component_kind,
   runtime, cost_tier, license_spdx. browse_components enumerates the
   registry without a keyword and supports filtering by kind, runtime,
   and cost_tier. Existing rows default to component_kind='library' so
   nothing downstream breaks."

Test coverage:
  1. Schema migration:
     - Existing capability rows have component_kind='library' by default.
     - Cost tier CHECK constraint rejects invalid values.
     - Component kind CHECK constraint rejects invalid values.
     - Interface input_type / output_type accept JSONB or NULL.
  2. search_capabilities extensions:
     - Filter by component_kind.
     - Filter by runtime.
     - Filter by cost_tier.
     - Row shape includes the new fields.
  3. browse_components:
     - No filters returns everything up to limit, ordered by score.
     - Filter by component_kind narrows correctly.
     - Filter by cost_tier narrows correctly.
     - Combined filters ANDed.
     - Limit clamped to [1, 500].
     - Empty registry returns [].
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from mcp_server import queries


# ---------------------------------------------------------------------------
# Minimal seed helpers (duplicated from test_step14 rather than shared —
# these tests need to control the new fields at insert time and the
# step14 helpers don't).
# ---------------------------------------------------------------------------

def _mk_component(
    conn,
    normalized_key: str,
    display_name: str,
    ecosystem: str | None = "pypi",
    capability_kind: str = "library",
    component_kind: str = "library",
    runtime: str | None = None,
    cost_tier: str | None = None,
    license_spdx: str | None = None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, "
            " component_kind, runtime, cost_tier, license_spdx) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (normalized_key, display_name, ecosystem, capability_kind,
             component_kind, runtime, cost_tier, license_spdx),
        )
        return cur.fetchone()[0]


def _mk_version(conn, cap_id: uuid.UUID, key: str = "content:v1") -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_version "
            "(capability_id, version_key, version_kind, display_version) "
            "VALUES (%s, %s, 'content-hash', '0.1.0') RETURNING id",
            (cap_id, key),
        )
        return cur.fetchone()[0]


def _mk_scoring_profile(conn) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scoring_profile (name, version, profile_hash, dimensions) "
            "VALUES (%s, 1, %s, '{}'::jsonb) RETURNING id",
            (f"prof-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex),
        )
        return cur.fetchone()[0]


def _mk_scorecard(conn, ver_id: uuid.UUID, profile_id: uuid.UUID, total: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scorecard "
            "(capability_version_id, scoring_profile_id, total_score, "
            " confidence, computed_hash) "
            "VALUES (%s, %s, %s, 0.9, %s)",
            (ver_id, profile_id, total, uuid.uuid4().hex),
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_existing_row_defaults_to_component_kind_library(conn):
    """A row inserted without specifying component_kind must get 'library'
    — that's the invariant that keeps pre-Phase-1 code working."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability (normalized_key, display_name, kind) "
            "VALUES ('pypi:legacy-row', 'legacy', 'library') RETURNING component_kind"
        )
        assert cur.fetchone()[0] == "library"
    conn.commit()


def test_component_kind_check_constraint_rejects_bogus_value(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capability "
                "(normalized_key, display_name, kind, component_kind) "
                "VALUES ('pypi:bad', 'bad', 'library', 'blueprint')"
            )


def test_cost_tier_check_constraint_rejects_bogus_value(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO capability "
                "(normalized_key, display_name, kind, cost_tier) "
                "VALUES ('pypi:bad', 'bad', 'library', 'expensive')"
            )


def test_cost_tier_allows_null(conn):
    _mk_component(conn, "pypi:no-cost-tier", "no-cost-tier")
    conn.commit()
    rows = queries.browse_components(conn, component_kind="library")
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:no-cost-tier" in keys


def test_interface_accepts_input_output_type_jsonb(conn):
    cap = _mk_component(conn, "pypi:iface", "iface")
    ver = _mk_version(conn, cap)
    # An interface still needs an evidence_item — reuse the pattern from
    # test_step14: analysis_run + evidence_item.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        )
        prov_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
            "VALUES (%s, 'a/b', 'a/b', 'repository') RETURNING id",
            (prov_id,),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'r1') RETURNING id",
            (asset_id,),
        )
        rev_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO analysis_run (source_revision_id, extractor_config_version, status) "
            "VALUES (%s, 1, 'completed') RETURNING id",
            (rev_id,),
        )
        run_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO evidence_item "
            "(source_revision_id, analysis_run_id, extractor_name, "
            " evidence_type, locator_kind, locator, extracted_value) "
            "VALUES (%s, %s, 'test', 'interface', 'whole_file', "
            " '{}'::jsonb, '{}'::jsonb) RETURNING id",
            (rev_id, run_id),
        )
        ev_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO capability_interface "
            "(capability_version_id, evidence_item_id, kind, name, "
            " signature, language, input_type, output_type) "
            "VALUES (%s, %s, 'function', 'transform', "
            " 'transform(img: Image) -> list[Tag]', 'python', "
            " %s::jsonb, %s::jsonb)",
            (ver, ev_id,
             json.dumps({"kind": "primitive", "type": "Image"}),
             json.dumps({"kind": "list", "of": "Tag"})),
        )
    conn.commit()

    detail = queries.capability_detail(conn, str(cap))
    iface = detail["interfaces"][0]
    assert iface["input_type"] == {"kind": "primitive", "type": "Image"}
    assert iface["output_type"] == {"kind": "list", "of": "Tag"}


# ---------------------------------------------------------------------------
# search_capabilities — new filters
# ---------------------------------------------------------------------------

def test_search_filter_by_component_kind(conn):
    _mk_component(conn, "pypi:libx", "libx", component_kind="library")
    _mk_component(conn, "source:github:acme/webapp", "acme-webapp",
                  ecosystem="source", component_kind="repo",
                  runtime="git_clone")
    conn.commit()

    rows = queries.search_capabilities(conn, query="", component_kind="repo")
    # Empty query short-circuits; use a broad substring instead.
    rows = queries.search_capabilities(conn, query="a", component_kind="repo")
    keys = [r["normalized_key"] for r in rows]
    assert "source:github:acme/webapp" in keys
    assert "pypi:libx" not in keys


def test_search_filter_by_runtime(conn):
    _mk_component(conn, "mcp:cip", "cip mcp", ecosystem="source",
                  component_kind="mcp_tool", runtime="mcp_stdio")
    _mk_component(conn, "skill:anthropic/code-review", "code-review",
                  ecosystem="source", component_kind="skill",
                  runtime="claude_skill")
    conn.commit()

    rows = queries.search_capabilities(conn, query="c", runtime="mcp_stdio")
    keys = [r["normalized_key"] for r in rows]
    assert "mcp:cip" in keys
    assert "skill:anthropic/code-review" not in keys


def test_search_filter_by_cost_tier(conn):
    _mk_component(conn, "pypi:freeA", "freeA", cost_tier="free")
    _mk_component(conn, "pypi:paidB", "paidB", cost_tier="paid")
    conn.commit()

    rows = queries.search_capabilities(conn, query="p", cost_tier="paid")
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:paidB" in keys
    assert "pypi:freeA" not in keys


def test_search_row_shape_includes_new_fields(conn):
    _mk_component(conn, "pypi:shape", "shape",
                  component_kind="library", runtime="python_import",
                  cost_tier="free", license_spdx="MIT")
    conn.commit()

    rows = queries.search_capabilities(conn, query="shape")
    assert len(rows) == 1
    row = rows[0]
    assert row["component_kind"] == "library"
    assert row["runtime"] == "python_import"
    assert row["cost_tier"] == "free"
    assert row["license_spdx"] == "MIT"


# ---------------------------------------------------------------------------
# browse_components
# ---------------------------------------------------------------------------

def test_browse_empty_registry_returns_empty_list(conn):
    assert queries.browse_components(conn) == []


def test_browse_no_filters_returns_everything_up_to_limit(conn):
    for i in range(3):
        _mk_component(conn, f"pypi:b{i}", f"b{i}")
    conn.commit()

    rows = queries.browse_components(conn)
    assert len(rows) == 3


def test_browse_filter_by_component_kind(conn):
    _mk_component(conn, "pypi:libA", "libA", component_kind="library")
    _mk_component(conn, "source:github:acme/repo", "acme-repo",
                  ecosystem="source", component_kind="repo")
    _mk_component(conn, "mcp:foo", "foo mcp", ecosystem="source",
                  component_kind="mcp_tool")
    conn.commit()

    repos = queries.browse_components(conn, component_kind="repo")
    assert [r["normalized_key"] for r in repos] == ["source:github:acme/repo"]

    mcps = queries.browse_components(conn, component_kind="mcp_tool")
    assert [r["normalized_key"] for r in mcps] == ["mcp:foo"]


def test_browse_filter_by_cost_tier(conn):
    _mk_component(conn, "pypi:free1", "free1", cost_tier="free")
    _mk_component(conn, "pypi:free2", "free2", cost_tier="free")
    _mk_component(conn, "pypi:paid1", "paid1", cost_tier="paid")
    conn.commit()

    free_rows = queries.browse_components(conn, cost_tier="free")
    assert {r["normalized_key"] for r in free_rows} == {"pypi:free1", "pypi:free2"}


def test_browse_combined_filters_are_anded(conn):
    _mk_component(conn, "source:github:acme/repoA", "repoA",
                  ecosystem="source", component_kind="repo",
                  runtime="git_clone", cost_tier="free")
    _mk_component(conn, "source:github:acme/repoB", "repoB",
                  ecosystem="source", component_kind="repo",
                  runtime="git_clone", cost_tier="paid")
    conn.commit()

    rows = queries.browse_components(
        conn, component_kind="repo", cost_tier="free"
    )
    assert [r["normalized_key"] for r in rows] == ["source:github:acme/repoA"]


def test_browse_orders_by_intrinsic_score(conn):
    cap_low = _mk_component(conn, "pypi:low", "low")
    cap_high = _mk_component(conn, "pypi:high", "high")
    ver_low = _mk_version(conn, cap_low)
    ver_high = _mk_version(conn, cap_high)
    profile = _mk_scoring_profile(conn)
    _mk_scorecard(conn, ver_low, profile, 0.30)
    _mk_scorecard(conn, ver_high, profile, 0.90)
    conn.commit()

    rows = queries.browse_components(conn)
    keys = [r["normalized_key"] for r in rows]
    assert keys.index("pypi:high") < keys.index("pypi:low")


def test_browse_limit_is_clamped(conn):
    _mk_component(conn, "pypi:one", "one")
    conn.commit()
    # < 1 clamps to 1
    assert len(queries.browse_components(conn, limit=0)) == 1
    assert len(queries.browse_components(conn, limit=-99)) == 1
    # > 500 doesn't crash (we only have 1 row anyway)
    assert len(queries.browse_components(conn, limit=99999)) == 1


# ---------------------------------------------------------------------------
# capability_detail — new fields
# ---------------------------------------------------------------------------

def test_detail_returns_new_metadata_fields(conn):
    cap = _mk_component(
        conn, "source:github:acme/thing", "acme-thing",
        ecosystem="source", capability_kind="library",
        component_kind="repo", runtime="git_clone",
        cost_tier="free", license_spdx="Apache-2.0",
    )
    conn.commit()

    detail = queries.capability_detail(conn, str(cap))
    assert detail["component_kind"] == "repo"
    assert detail["runtime"] == "git_clone"
    assert detail["cost_tier"] == "free"
    assert detail["license_spdx"] == "Apache-2.0"
