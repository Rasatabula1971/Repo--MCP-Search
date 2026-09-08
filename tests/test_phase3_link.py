"""
Phase 3b done-when:

  "For each shared-tail-name pair of (library, repo) capabilities,
   the linker records a capability_link row of kind 'same_component'
   with the model's decision, provenance, and confidence. Confidence
   above LINK_THRESHOLD writes a real link; below or 'not same'
   stores a suppressed row so the pair isn't re-asked next run."
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from scripts.judge import link_identities as li


# ---------------------------------------------------------------------------
# _tail_name
# ---------------------------------------------------------------------------

def test_tail_name_from_pypi():
    assert li._tail_name("pypi:requests") == "requests"


def test_tail_name_from_npm():
    assert li._tail_name("npm:react-hook-form") == "react-hook-form"


def test_tail_name_from_github_source():
    assert li._tail_name("source:github:psf/requests") == "requests"
    assert li._tail_name("source:github:openshot/openshot-qt") == "openshot-qt"


def test_tail_name_lowercases():
    assert li._tail_name("source:github:PSF/Requests") == "requests"


def test_tail_name_returns_none_for_malformed():
    assert li._tail_name("not-a-key") is None
    assert li._tail_name("") is None


# ---------------------------------------------------------------------------
# _pair_key
# ---------------------------------------------------------------------------

def test_pair_key_is_direction_agnostic():
    assert li._pair_key("aaa", "bbb") == li._pair_key("bbb", "aaa")


# ---------------------------------------------------------------------------
# find_link_candidates
# ---------------------------------------------------------------------------

def _mk(conn, key, component_kind, capability_kind="library"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES (%s, %s, 'source', %s, %s) RETURNING id::text",
            (key, key, capability_kind, component_kind),
        )
        return cur.fetchone()[0]


def test_find_candidates_matches_shared_tail(conn):
    lib_id = _mk(conn, "pypi:requests", "library")
    repo_id = _mk(conn, "source:github:psf/requests", "repo")
    _mk(conn, "source:github:openshot/openshot-qt", "repo")  # unrelated
    conn.commit()

    pairs = li.find_link_candidates(conn, limit=10, force=False)
    assert len(pairs) == 1
    a, b = pairs[0]
    assert {a["id"], b["id"]} == {lib_id, repo_id}


def test_find_candidates_returns_empty_when_no_overlap(conn):
    _mk(conn, "pypi:foo", "library")
    _mk(conn, "source:github:x/y", "repo")
    conn.commit()
    assert li.find_link_candidates(conn, limit=10, force=False) == []


def test_find_candidates_excludes_pairs_already_asked_at_current_version(conn):
    lib_id = _mk(conn, "pypi:requests", "library")
    repo_id = _mk(conn, "source:github:psf/requests", "repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_link "
            "(source_capability_id, target_capability_id, link_kind, "
            " confidence, rationale, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, 'same_component', 0.9, 'ok', %s, %s, 'h', 'm')",
            (lib_id, repo_id, li.PROMPT_NAME, li.PROMPT_VERSION),
        )
    conn.commit()

    assert li.find_link_candidates(conn, limit=10, force=False) == []


def test_find_candidates_force_returns_already_asked(conn):
    lib_id = _mk(conn, "pypi:requests", "library")
    repo_id = _mk(conn, "source:github:psf/requests", "repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_link "
            "(source_capability_id, target_capability_id, link_kind, "
            " confidence, rationale, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, 'same_component', 0.9, 'ok', %s, %s, 'h', 'm')",
            (lib_id, repo_id, li.PROMPT_NAME, li.PROMPT_VERSION),
        )
    conn.commit()

    assert len(li.find_link_candidates(conn, limit=10, force=True)) == 1


def test_find_candidates_prior_version_does_not_suppress(conn):
    lib_id = _mk(conn, "pypi:requests", "library")
    repo_id = _mk(conn, "source:github:psf/requests", "repo")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_link "
            "(source_capability_id, target_capability_id, link_kind, "
            " confidence, rationale, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, 'same_component', 0.9, 'ok', %s, %s, 'h', 'm')",
            (lib_id, repo_id, li.PROMPT_NAME, li.PROMPT_VERSION - 1),
        )
    conn.commit()

    assert len(li.find_link_candidates(conn, limit=10, force=False)) == 1


def test_find_candidates_ignores_pairs_of_two_libraries(conn):
    """A pypi and an npm package sharing a name (react ↔ react) are
    NOT the same component — we only pair library ↔ repo."""
    _mk(conn, "pypi:react", "library")
    _mk(conn, "npm:react", "library")
    conn.commit()
    assert li.find_link_candidates(conn, limit=10, force=False) == []


# ---------------------------------------------------------------------------
# parse_decision
# ---------------------------------------------------------------------------

def _dec(**overrides) -> str:
    obj = {"same_component": True, "confidence": 0.9, "rationale": "matches"}
    obj.update(overrides)
    return json.dumps(obj)


def test_parse_valid_true():
    d = li.parse_decision(_dec())
    assert d.same_component is True
    assert d.confidence == 0.9


def test_parse_valid_false():
    d = li.parse_decision(_dec(same_component=False, confidence=0.85))
    assert d.same_component is False


def test_parse_strips_markdown_fence():
    d = li.parse_decision("```json\n" + _dec() + "\n```")
    assert d.same_component is True


def test_parse_rejects_non_json():
    with pytest.raises(li.MalformedResponse, match="not_json"):
        li.parse_decision("nope")


def test_parse_rejects_non_bool_same_component():
    with pytest.raises(li.MalformedResponse, match="bad_same_component"):
        li.parse_decision(_dec(same_component="maybe"))


def test_parse_rejects_confidence_out_of_range():
    with pytest.raises(li.MalformedResponse, match="confidence_out_of_range"):
        li.parse_decision(_dec(confidence=2.0))


# ---------------------------------------------------------------------------
# call_gemini (mocked)
# ---------------------------------------------------------------------------

class _MockClient:
    def __init__(self, resp=None, status=200):
        self.resp = resp or {"candidates": [{"content": {"parts": [{"text": _dec()}]}}]}
        self.status = status

    def post(self, url, params=None, json=None):
        return httpx.Response(self.status, json=self.resp,
                                request=httpx.Request("POST", url))


def test_call_gemini_returns_text():
    m = _MockClient()
    assert json.loads(li.call_gemini("p", "k", client=m))["same_component"] is True


def test_call_gemini_raises_on_rate_limit():
    m = _MockClient({"error": {}}, status=429)
    with pytest.raises(li.GeminiError, match="rate_limited"):
        li.call_gemini("p", "k", client=m)


# ---------------------------------------------------------------------------
# _record_link
# ---------------------------------------------------------------------------

def _link_rows(conn, cap_a, cap_b):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT link_kind, confidence, rationale FROM capability_link "
            "WHERE (source_capability_id::text = %s AND target_capability_id::text = %s) "
            "   OR (source_capability_id::text = %s AND target_capability_id::text = %s)",
            (cap_a, cap_b, cap_b, cap_a),
        )
        return cur.fetchall()


def test_record_link_high_confidence_writes_positive_link(conn):
    a = _mk(conn, "pypi:requests", "library")
    b = _mk(conn, "source:github:psf/requests", "repo")
    conn.commit()

    outcome = li._record_link(
        conn,
        {"id": a, "normalized_key": "pypi:requests"},
        {"id": b, "normalized_key": "source:github:psf/requests"},
        li.LinkDecision(same_component=True, confidence=0.9, rationale="matches"),
        "hashx",
    )
    conn.commit()
    assert outcome == "linked"
    rows = _link_rows(conn, a, b)
    assert len(rows) == 1
    assert float(rows[0][1]) == pytest.approx(0.9)
    assert not rows[0][2].startswith("NOT_SAME")


def test_record_link_not_same_writes_negative_row_so_pair_not_re_asked(conn):
    a = _mk(conn, "pypi:requests", "library")
    b = _mk(conn, "source:github:other/requests", "repo")
    conn.commit()

    outcome = li._record_link(
        conn,
        {"id": a, "normalized_key": "pypi:requests"},
        {"id": b, "normalized_key": "source:github:other/requests"},
        li.LinkDecision(same_component=False, confidence=0.85,
                         rationale="different maintainers, different code"),
        "hashy",
    )
    conn.commit()
    assert outcome == "not_same"
    rows = _link_rows(conn, a, b)
    assert len(rows) == 1
    assert rows[0][2].startswith("NOT_SAME:")


def test_record_link_low_confidence_true_is_suppressed_but_recorded(conn):
    a = _mk(conn, "pypi:requests", "library")
    b = _mk(conn, "source:github:psf/requests", "repo")
    conn.commit()

    outcome = li._record_link(
        conn,
        {"id": a, "normalized_key": "pypi:requests"},
        {"id": b, "normalized_key": "source:github:psf/requests"},
        li.LinkDecision(same_component=True, confidence=0.55, rationale="unsure"),
        "hashz",
    )
    conn.commit()
    assert outcome == "suppressed_low_confidence"
    # Row is still there — so the candidate filter finds it and skips.
    assert len(_link_rows(conn, a, b)) == 1
