"""
Phase 3d done-when:

  "score_component picks the right per-kind scorer, produces a
   ScoreOutcome with dimensions that sum to total, and upsert_score
   is idempotent — re-running at the same profile_version writes no
   changes."
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.score import component_scores as sc


# ---------------------------------------------------------------------------
# Primitive scorers
# ---------------------------------------------------------------------------

def test_freshness_full_score_for_today():
    d = sc._freshness_score(0.0)
    assert d.raw == 1.0
    assert d.has_evidence is True


def test_freshness_half_score_for_365_days():
    d = sc._freshness_score(365.0)
    assert d.raw == pytest.approx(0.5, abs=0.01)


def test_freshness_zero_for_very_old():
    d = sc._freshness_score(3000.0)
    assert d.raw == 0.0


def test_freshness_defaults_when_unknown():
    d = sc._freshness_score(None)
    assert d.raw == 0.5
    assert d.has_evidence is False


def test_popularity_log_normalized():
    assert sc._popularity_score(0).raw == 0.0
    assert 0.4 < sc._popularity_score(100).raw < 0.6      # ~log10(101)/4
    assert sc._popularity_score(20000).raw == 1.0


def test_popularity_unknown_defaults():
    d = sc._popularity_score(None)
    assert d.raw == 0.5
    assert d.has_evidence is False


def test_license_clarity_known_vs_unknown():
    assert sc._license_clarity_score("MIT").raw == 1.0
    assert sc._license_clarity_score(None).raw == 0.3


def test_description_quality_tiers():
    assert sc._description_quality_score({"gemini_description": "x"}).raw == 1.0
    assert sc._description_quality_score({"description": "y"}).raw == 0.6
    assert sc._description_quality_score({}).raw == 0.0


def test_transport_clarity_known_transports():
    assert sc._transport_clarity_score({"transport": "stdio"}).raw == 1.0
    assert sc._transport_clarity_score({"transport": "sse"}).raw == 1.0
    assert sc._transport_clarity_score({}).raw == 0.3


def test_ecosystem_bonus_pypi_vs_source():
    assert sc._ecosystem_bonus_score("pypi").raw == 1.0
    assert sc._ecosystem_bonus_score("npm").raw == 1.0
    assert sc._ecosystem_bonus_score("source").raw == 0.5
    assert sc._ecosystem_bonus_score("unknown").raw == 0.3


def test_pushed_at_parses_iso_z_suffix():
    two_days = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    d = sc._pushed_at_days_ago({"pushed_at": two_days})
    assert 1.5 < d < 2.5


def test_pushed_at_returns_none_when_absent():
    assert sc._pushed_at_days_ago({}) is None


# ---------------------------------------------------------------------------
# Per-kind scorers
# ---------------------------------------------------------------------------

def _fresh_iso(days_ago: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace(
        "+00:00", "Z")


def test_score_repo_returns_all_five_dimensions_and_weights_sum_to_1():
    cap = {"component_kind": "repo", "ecosystem": "source",
           "license_spdx": "MIT",
           "metadata": {"stars": 1000, "forks": 50, "open_issues": 5,
                        "pushed_at": _fresh_iso(30),
                        "gemini_description": "does X"}}
    out = sc._score_repo(cap)
    assert out.profile_name == "repo_health"
    assert set(out.dimensions.keys()) == {
        "freshness", "popularity", "activity",
        "license_clarity", "description_quality",
    }
    assert round(sum(d.weight for d in out.dimensions.values()), 4) == 1.0
    # And total matches manual weighted sum.
    total = round(sum(d.weighted for d in out.dimensions.values()), 3)
    assert out.total == total
    assert 0.0 <= out.total <= 1.0


def test_score_library_weights_sum_to_1():
    cap = {"component_kind": "library", "ecosystem": "pypi",
           "license_spdx": "Apache-2.0",
           "metadata": {"gemini_description": "does X"}}
    out = sc._score_library(cap)
    assert out.profile_name == "library_health"
    assert round(sum(d.weight for d in out.dimensions.values()), 4) == 1.0


def test_score_skill_weights_sum_to_1():
    cap = {"component_kind": "skill", "ecosystem": "source",
           "license_spdx": None,
           "metadata": {"gemini_description": "d", "content_hash": "h",
                        "html_url": "u"}}
    out = sc._score_skill(cap)
    assert out.profile_name == "skill_health"
    assert round(sum(d.weight for d in out.dimensions.values()), 4) == 1.0


def test_score_mcp_tool_weights_sum_to_1():
    cap = {"component_kind": "mcp_tool", "ecosystem": "source",
           "license_spdx": None,
           "metadata": {"transport": "stdio", "gemini_description": "d"}}
    out = sc._score_mcp_tool(cap)
    assert out.profile_name == "mcp_tool_health"
    assert round(sum(d.weight for d in out.dimensions.values()), 4) == 1.0


def test_score_component_dispatches_by_kind():
    for kind, expected_profile in [
        ("repo", "repo_health"),
        ("library", "library_health"),
        ("skill", "skill_health"),
        ("mcp_tool", "mcp_tool_health"),
        ("agent", "agent_health"),
        ("workflow_template", "workflow_template_health"),
    ]:
        cap = {"component_kind": kind, "ecosystem": "source",
               "license_spdx": None, "metadata": {}}
        out = sc.score_component(cap)
        assert out is not None
        assert out.profile_name == expected_profile


def test_score_component_returns_none_for_unknown_kind():
    cap = {"component_kind": "mystery", "ecosystem": "source",
           "license_spdx": None, "metadata": {}}
    assert sc.score_component(cap) is None


def test_confidence_reflects_evidence_coverage():
    """A repo with no stars/pushed_at data should have lower confidence
    than one with full metadata."""
    thin = {"component_kind": "repo", "ecosystem": "source",
             "license_spdx": None, "metadata": {}}
    full = {"component_kind": "repo", "ecosystem": "source",
             "license_spdx": "MIT",
             "metadata": {"stars": 100, "forks": 2, "open_issues": 1,
                          "pushed_at": _fresh_iso(30),
                          "gemini_description": "x"}}
    assert sc.score_component(thin).confidence < sc.score_component(full).confidence
    assert sc.score_component(full).confidence == 1.0


# ---------------------------------------------------------------------------
# upsert_score
# ---------------------------------------------------------------------------

def _mk_cap(conn, kind="repo"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES (%s, 'x', 'source', 'library', %s) RETURNING id::text",
            (f"source:test:{uuid.uuid4().hex[:8]}", kind),
        )
        return cur.fetchone()[0]


def _score_row(conn, cap_id, profile_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_score, confidence, dimensions FROM component_score "
            "WHERE capability_id::text = %s AND profile_name = %s "
            "  AND profile_version = %s",
            (cap_id, profile_name, sc.PROFILE_VERSION),
        )
        return cur.fetchone()


def test_upsert_new_row(conn):
    cid = _mk_cap(conn, "repo")
    conn.commit()
    outcome = sc.ScoreOutcome(profile_name="repo_health", total=0.5, confidence=1.0,
                               dimensions={})
    assert sc.upsert_score(conn, cid, outcome) == "new"
    conn.commit()
    row = _score_row(conn, cid, "repo_health")
    assert float(row[0]) == 0.5


def test_upsert_unchanged_when_values_match(conn):
    cid = _mk_cap(conn, "repo")
    conn.commit()
    outcome = sc.ScoreOutcome(profile_name="repo_health", total=0.5, confidence=1.0,
                               dimensions={})
    sc.upsert_score(conn, cid, outcome)
    conn.commit()
    assert sc.upsert_score(conn, cid, outcome) == "unchanged"


def test_upsert_updated_when_score_changes(conn):
    cid = _mk_cap(conn, "repo")
    conn.commit()
    sc.upsert_score(conn, cid,
                     sc.ScoreOutcome(profile_name="repo_health", total=0.5,
                                      confidence=1.0, dimensions={}))
    conn.commit()
    outcome = sc.ScoreOutcome(profile_name="repo_health", total=0.7, confidence=1.0,
                               dimensions={})
    assert sc.upsert_score(conn, cid, outcome) == "updated"
    conn.commit()
    row = _score_row(conn, cid, "repo_health")
    assert float(row[0]) == 0.7
