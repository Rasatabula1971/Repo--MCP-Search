"""
Phase 3a done-when:

  "Every classification an LLM makes is recorded in component_
   classification with prompt_name + prompt_version + prompt_hash for
   full audit. Confidence at or above APPLY_THRESHOLD with a real
   diff mutates capability.component_kind + capability.kind. Below
   threshold or a no-op are suppressed but still recorded."

Tests operate on the parser and the DB record/apply logic. Live Gemini
calls are not made; the client is mocked.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from scripts.judge import classify_components as cc


# ---------------------------------------------------------------------------
# parse_classification
# ---------------------------------------------------------------------------

def _valid_json(**overrides) -> str:
    obj = {
        "component_kind": "repo",
        "capability_kind": "tool",
        "purpose": "does the thing",
        "confidence": 0.85,
        "rationale": "readme says so",
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_parse_valid_response():
    cls = cc.parse_classification(_valid_json())
    assert cls.component_kind == "repo"
    assert cls.capability_kind == "tool"
    assert cls.confidence == 0.85


def test_parse_strips_markdown_fences():
    fenced = "```json\n" + _valid_json() + "\n```"
    cls = cc.parse_classification(fenced)
    assert cls.component_kind == "repo"


def test_parse_lowercases_kinds():
    cls = cc.parse_classification(_valid_json(component_kind="LIBRARY",
                                                capability_kind="Service"))
    assert cls.component_kind == "library"
    assert cls.capability_kind == "service"


def test_parse_rejects_non_json():
    with pytest.raises(cc.MalformedResponse, match="not_json"):
        cc.parse_classification("not json at all")


def test_parse_rejects_non_object():
    with pytest.raises(cc.MalformedResponse, match="not_object"):
        cc.parse_classification('["a", "b"]')


def test_parse_rejects_missing_field():
    with pytest.raises(cc.MalformedResponse, match="missing_field"):
        obj = json.loads(_valid_json())
        del obj["confidence"]
        cc.parse_classification(json.dumps(obj))


def test_parse_rejects_unknown_component_kind():
    with pytest.raises(cc.MalformedResponse, match="bad_component_kind"):
        cc.parse_classification(_valid_json(component_kind="blueprint"))


def test_parse_rejects_unknown_capability_kind():
    with pytest.raises(cc.MalformedResponse, match="bad_capability_kind"):
        cc.parse_classification(_valid_json(capability_kind="magic"))


def test_parse_rejects_confidence_out_of_range():
    with pytest.raises(cc.MalformedResponse, match="confidence_out_of_range"):
        cc.parse_classification(_valid_json(confidence=1.5))


def test_parse_rejects_non_numeric_confidence():
    with pytest.raises(cc.MalformedResponse, match="bad_confidence"):
        cc.parse_classification(_valid_json(confidence="high"))


def test_parse_truncates_long_purpose_and_rationale():
    long = "x" * 500
    cls = cc.parse_classification(_valid_json(purpose=long, rationale=long))
    assert len(cls.purpose) <= 280
    assert len(cls.rationale) <= 280


# ---------------------------------------------------------------------------
# call_gemini (mocked)
# ---------------------------------------------------------------------------

class _MockClient:
    def __init__(self, response=None, status=200):
        self.response = response or {
            "candidates": [{"content": {"parts": [{"text": _valid_json()}]}}]
        }
        self.status = status

    def post(self, url, params=None, json=None):
        return httpx.Response(self.status, json=self.response,
                                request=httpx.Request("POST", url))


def test_call_gemini_returns_response_text():
    m = _MockClient()
    text = cc.call_gemini("prompt", "key", client=m)
    assert json.loads(text)["component_kind"] == "repo"


def test_call_gemini_raises_on_rate_limit():
    m = _MockClient({"error": {"code": 429}}, status=429)
    with pytest.raises(cc.GeminiError, match="rate_limited"):
        cc.call_gemini("p", "k", client=m)


# ---------------------------------------------------------------------------
# _record_and_maybe_apply
# ---------------------------------------------------------------------------

def _mk_cap(conn, component_kind="repo", capability_kind="library"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES (%s, 'x', 'source', %s, %s) RETURNING id::text",
            (f"source:test:{uuid.uuid4().hex[:8]}", capability_kind, component_kind),
        )
        return cur.fetchone()[0]


def _cap_state(conn, cap_id):
    with conn.cursor() as cur:
        cur.execute("SELECT component_kind, kind FROM capability WHERE id::text = %s",
                    (cap_id,))
        return cur.fetchone()


def _classification_row(conn, cap_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT proposed_component_kind, proposed_capability_kind, "
            "       confidence, applied, apply_reason, "
            "       prior_component_kind, prior_capability_kind, "
            "       prompt_name, prompt_version "
            "FROM component_classification WHERE capability_id::text = %s",
            (cap_id,),
        )
        return cur.fetchone()


def test_high_confidence_diff_applies_change(conn):
    cid = _mk_cap(conn, component_kind="repo", capability_kind="library")
    conn.commit()

    cap = {"id": cid, "component_kind": "repo", "kind": "library",
           "normalized_key": "x"}
    cls = cc.Classification("mcp_tool", "service", "does mcp things", 0.95, "ok")
    outcome = cc._record_and_maybe_apply(conn, cap, cls, "abc123")
    conn.commit()

    assert outcome == "applied_change"
    assert _cap_state(conn, cid) == ("mcp_tool", "service")
    row = _classification_row(conn, cid)
    assert row[3] is True             # applied
    assert row[4] == "high_confidence"
    assert row[5] == "repo"           # prior_component_kind snapshot
    assert row[6] == "library"


def test_high_confidence_no_diff_records_but_does_not_apply(conn):
    cid = _mk_cap(conn, component_kind="library", capability_kind="library")
    conn.commit()

    cap = {"id": cid, "component_kind": "library", "kind": "library",
           "normalized_key": "x"}
    cls = cc.Classification("library", "library", "yep", 0.95, "ok")
    outcome = cc._record_and_maybe_apply(conn, cap, cls, "abc")
    conn.commit()

    assert outcome == "applied_noop"
    assert _cap_state(conn, cid) == ("library", "library")   # unchanged
    row = _classification_row(conn, cid)
    assert row[3] is False
    assert row[4] == "no_change"


def test_low_confidence_diff_records_but_does_not_apply(conn):
    cid = _mk_cap(conn, component_kind="repo", capability_kind="library")
    conn.commit()

    cap = {"id": cid, "component_kind": "repo", "kind": "library",
           "normalized_key": "x"}
    cls = cc.Classification("mcp_tool", "service", "maybe", 0.50, "unsure")
    outcome = cc._record_and_maybe_apply(conn, cap, cls, "abc")
    conn.commit()

    assert outcome == "suppressed_low_confidence"
    assert _cap_state(conn, cid) == ("repo", "library")   # unchanged
    row = _classification_row(conn, cid)
    assert row[3] is False
    assert row[4] == "suppressed_low_confidence"


# ---------------------------------------------------------------------------
# _candidates
# ---------------------------------------------------------------------------

def test_candidates_skips_already_classified_at_same_prompt_version(conn):
    cid = _mk_cap(conn, component_kind="repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " confidence, applied, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, 'repo', 'library', 0.9, false, %s, %s, 'h', 'm')",
            (cid, cc.PROMPT_NAME, cc.PROMPT_VERSION),
        )
    conn.commit()

    rows = cc._candidates(conn, kind=None, limit=100, force=False)
    assert cid not in [r["id"] for r in rows]


def test_candidates_returns_row_when_prior_prompt_version_differs(conn):
    cid = _mk_cap(conn, component_kind="repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " confidence, applied, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, 'repo', 'library', 0.9, false, %s, %s, 'h', 'm')",
            (cid, cc.PROMPT_NAME, cc.PROMPT_VERSION - 1),
        )
    conn.commit()

    rows = cc._candidates(conn, kind=None, limit=100, force=False)
    assert cid in [r["id"] for r in rows]


def test_candidates_force_returns_already_classified(conn):
    cid = _mk_cap(conn, component_kind="repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " confidence, applied, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, 'repo', 'library', 0.9, false, %s, %s, 'h', 'm')",
            (cid, cc.PROMPT_NAME, cc.PROMPT_VERSION),
        )
    conn.commit()

    rows = cc._candidates(conn, kind=None, limit=100, force=True)
    assert cid in [r["id"] for r in rows]


def test_candidates_filters_by_kind(conn):
    _mk_cap(conn, component_kind="repo")
    lib_id = _mk_cap(conn, component_kind="library")
    conn.commit()

    rows = cc._candidates(conn, kind="library", limit=100, force=False)
    keys = [r["id"] for r in rows]
    assert lib_id in keys
    assert len(keys) == 1
