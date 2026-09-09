"""
Phase 5b done-when:

  "Given an intent, parse_stages accepts the LLM's structured JSON
   response, find_candidates_for_stage returns real registry rows
   for a stage's search terms, and suggest_pipeline (via mocked
   Gemini) returns a proposal with per-stage picks + adjacent-stage
   compatibility edges + gap notes when no candidates exist."
"""
from __future__ import annotations

import json
import uuid
from unittest import mock

import httpx
import pytest

from scripts.compose import suggest_pipeline as sp


# ---------------------------------------------------------------------------
# parse_stages
# ---------------------------------------------------------------------------

def _stages_json(*names) -> str:
    return json.dumps({
        "stages": [
            {"name": n, "purpose": f"does {n}",
             "search_terms": [n, n.replace("-", " ")],
             "role": "producer",
             "preferred_component_kind": "library"}
            for n in names
        ]
    })


def test_parse_stages_returns_ordered_list():
    stages = sp.parse_stages(_stages_json("a", "b", "c"))
    assert [s.name for s in stages] == ["a", "b", "c"]


def test_parse_stages_strips_markdown_fence():
    fenced = "```json\n" + _stages_json("x") + "\n```"
    assert len(sp.parse_stages(fenced)) == 1


def test_parse_stages_rejects_non_json():
    with pytest.raises(sp.MalformedResponse, match="not_json"):
        sp.parse_stages("hi")


def test_parse_stages_rejects_missing_stages_field():
    with pytest.raises(sp.MalformedResponse, match="missing_stages_field"):
        sp.parse_stages("{}")


def test_parse_stages_rejects_empty_stages_list():
    with pytest.raises(sp.MalformedResponse, match="stages_not_a_nonempty_list"):
        sp.parse_stages(json.dumps({"stages": []}))


def test_parse_stages_rejects_missing_stage_field():
    bad = json.dumps({"stages": [
        {"name": "x", "purpose": "y", "search_terms": ["z"], "role": "producer"}
        # missing preferred_component_kind
    ]})
    with pytest.raises(sp.MalformedResponse, match="missing_preferred_component_kind"):
        sp.parse_stages(bad)


def test_parse_stages_rejects_bad_role():
    bad = json.dumps({"stages": [
        {"name": "x", "purpose": "y", "search_terms": ["z"],
         "role": "wizard", "preferred_component_kind": "library"}
    ]})
    with pytest.raises(sp.MalformedResponse, match="bad_role"):
        sp.parse_stages(bad)


def test_parse_stages_rejects_bad_kind():
    bad = json.dumps({"stages": [
        {"name": "x", "purpose": "y", "search_terms": ["z"],
         "role": "producer", "preferred_component_kind": "wardrobe"}
    ]})
    with pytest.raises(sp.MalformedResponse, match="bad_kind"):
        sp.parse_stages(bad)


def test_parse_stages_lowercases_role_and_kind():
    good = json.dumps({"stages": [
        {"name": "x", "purpose": "y", "search_terms": ["z"],
         "role": "PRODUCER", "preferred_component_kind": "Library"}
    ]})
    stages = sp.parse_stages(good)
    assert stages[0].role == "producer"
    assert stages[0].preferred_component_kind == "library"


def test_parse_stages_truncates_long_name_and_purpose():
    long = "x" * 500
    bad = json.dumps({"stages": [
        {"name": long, "purpose": long, "search_terms": ["z"],
         "role": "producer", "preferred_component_kind": "library"}
    ]})
    stages = sp.parse_stages(bad)
    assert len(stages[0].name) <= 60
    assert len(stages[0].purpose) <= 280


# ---------------------------------------------------------------------------
# call_gemini (mocked)
# ---------------------------------------------------------------------------

class _MockClient:
    def __init__(self, resp=None, status=200):
        self.resp = resp or {"candidates": [{"content": {"parts": [
            {"text": _stages_json("stage-1", "stage-2")}
        ]}}]}
        self.status = status

    def post(self, url, params=None, json=None):
        return httpx.Response(self.status, json=self.resp,
                                request=httpx.Request("POST", url))


def test_call_gemini_returns_text():
    m = _MockClient()
    got = sp.call_gemini("prompt", "key", client=m)
    parsed = sp.parse_stages(got)
    assert len(parsed) == 2


def test_call_gemini_raises_on_rate_limit():
    m = _MockClient({"error": {}}, status=429)
    with pytest.raises(sp.GeminiError, match="rate_limited"):
        sp.call_gemini("p", "k", client=m)


# ---------------------------------------------------------------------------
# find_candidates_for_stage — DB-backed
# ---------------------------------------------------------------------------

def _mk(conn, key, kind="library", score=None, runtime="python_import"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, "
            " component_kind, runtime, cost_tier, license_spdx) "
            "VALUES (%s, %s, 'source', 'library', %s, %s, 'free', 'MIT') "
            "RETURNING id::text",
            (key, key, kind, runtime),
        )
        cap_id = cur.fetchone()[0]
        if score is not None:
            # A component_score row so ordering has something to use.
            cur.execute(
                "INSERT INTO scoring_profile (name, version, profile_hash, dimensions) "
                "VALUES (%s, 1, %s, '{}'::jsonb) RETURNING id",
                (f"prof-{uuid.uuid4().hex[:6]}", uuid.uuid4().hex),
            )
            prof_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO capability_version "
                "(capability_id, version_key, version_kind) "
                "VALUES (%s, %s, 'content-hash') RETURNING id",
                (cap_id, f"content:{uuid.uuid4().hex[:8]}"),
            )
            ver_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO scorecard "
                "(capability_version_id, scoring_profile_id, "
                " total_score, confidence, computed_hash) "
                "VALUES (%s, %s, %s, 0.9, %s)",
                (ver_id, prof_id, score, uuid.uuid4().hex),
            )
        return cap_id


def test_find_candidates_finds_by_any_search_term(conn):
    _mk(conn, "pypi:transcribe-audio", "library", score=0.8)
    _mk(conn, "pypi:whisper-model",    "library", score=0.9)
    _mk(conn, "pypi:unrelated-thing",  "library", score=0.5)
    conn.commit()

    stage = sp.ProposedStage(
        name="transcribe", purpose="STT",
        search_terms=["whisper", "transcribe"],
        role="transformer", preferred_component_kind="library",
    )
    got = sp.find_candidates_for_stage(conn, stage, project_id=None,
                                         per_stage=3)
    keys = [r["normalized_key"] for r in got]
    assert "pypi:whisper-model" in keys
    assert "pypi:transcribe-audio" in keys
    assert "pypi:unrelated-thing" not in keys


def test_find_candidates_ordered_by_score(conn):
    _mk(conn, "pypi:low-scoring-transcribe",  "library", score=0.3)
    _mk(conn, "pypi:high-scoring-transcribe", "library", score=0.95)
    conn.commit()

    stage = sp.ProposedStage(
        name="t", purpose="STT", search_terms=["transcribe"],
        role="transformer", preferred_component_kind="library",
    )
    got = sp.find_candidates_for_stage(conn, stage, project_id=None, per_stage=5)
    assert got[0]["normalized_key"] == "pypi:high-scoring-transcribe"


def test_find_candidates_returns_empty_when_no_match(conn):
    stage = sp.ProposedStage(
        name="none", purpose="none", search_terms=["nothing-in-registry"],
        role="producer", preferred_component_kind="library",
    )
    assert sp.find_candidates_for_stage(conn, stage, None) == []


# ---------------------------------------------------------------------------
# End-to-end via mocked Gemini
# ---------------------------------------------------------------------------

def test_suggest_pipeline_end_to_end_with_mocked_gemini(conn, monkeypatch):
    _mk(conn, "pypi:one",   "library", score=0.9)
    _mk(conn, "pypi:two",   "library", score=0.9)
    conn.commit()

    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(sp, "call_gemini",
                         lambda prompt, key, client=None: _stages_json("one", "two"))

    result = sp.suggest_pipeline(intent="test intent", project_id=None,
                                   max_stages=5, conn=conn)
    assert result.intent == "test intent"
    assert len(result.stages) == 2
    assert result.stages[0]["pick"]["normalized_key"] == "pypi:one"
    assert result.stages[1]["pick"]["normalized_key"] == "pypi:two"
    # One adjacent edge, both picks python_import -> compatible.
    assert len(result.edges) == 1
    assert result.edges[0]["verdict"] == "compatible"


def test_suggest_pipeline_records_gap_note_when_stage_has_no_candidates(
    conn, monkeypatch,
):
    # No rows in registry — every stage will come up empty.
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(sp, "call_gemini",
                         lambda p, k, client=None: _stages_json("missing"))

    result = sp.suggest_pipeline(intent="x", max_stages=3, conn=conn)
    assert result.stages[0]["pick"] is None
    assert any("NO candidates" in n for n in result.notes)
