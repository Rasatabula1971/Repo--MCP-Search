"""
Phase 2 done-when for the Gemini enricher:

  "For a component that lacks a gemini_description in metadata, the
   enricher builds a compact prompt from what we know, calls Gemini
   once, and writes the answer back into metadata under
   gemini_description + gemini_prompt_hash + gemini_model. Re-runs
   skip already-enriched components. Live Gemini calls are NOT made
   from tests — the client is mocked."
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from scripts.ingest import gemini_enrich


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_build_prompt_includes_known_fields():
    cap = {
        "normalized_key": "source:github:acme/thing",
        "display_name": "acme/thing",
        "component_kind": "repo",
        "metadata": {
            "description": "does things",
            "topics": ["video", "editing"],
            "language": "Python",
            "html_url": "https://github.com/acme/thing",
        },
    }
    prompt = gemini_enrich._build_prompt(cap)
    assert "acme/thing" in prompt
    assert "component_kind: repo" in prompt
    assert "does things" in prompt
    assert "video, editing" in prompt
    assert "Python" in prompt


def test_build_prompt_truncates_long_description():
    cap = {
        "normalized_key": "x",
        "display_name": "x",
        "component_kind": "library",
        "metadata": {"description": "a" * 5000},
    }
    prompt = gemini_enrich._build_prompt(cap)
    # 400-char cap enforced.
    assert len([line for line in prompt.splitlines()
                if line.startswith("existing_description:")][0]) <= 500


def test_prompt_hash_is_stable_and_length_16():
    text = "same input"
    h1 = gemini_enrich._prompt_hash(text)
    h2 = gemini_enrich._prompt_hash(text)
    assert h1 == h2
    assert len(h1) == 16
    assert h1 != gemini_enrich._prompt_hash("different input")


# ---------------------------------------------------------------------------
# call_gemini (mocked)
# ---------------------------------------------------------------------------

class _MockClient:
    def __init__(self, response_json=None, status=200):
        self.response_json = response_json or {
            "candidates": [
                {"content": {"parts": [{"text": "does the thing succinctly."}]}}
            ]
        }
        self.status = status
        self.calls = []

    def post(self, url, params=None, json=None):
        self.calls.append((url, params, json))
        resp = httpx.Response(self.status, json=self.response_json,
                               request=httpx.Request("POST", url))
        return resp


def test_call_gemini_returns_stripped_text():
    m = _MockClient({
        "candidates": [{"content": {"parts": [{"text": "  hello world  \n"}]}}]
    })
    got = gemini_enrich.call_gemini("prompt", "fake-key", client=m)
    assert got == "hello world"


def test_call_gemini_raises_on_rate_limit():
    m = _MockClient({"error": {"code": 429}}, status=429)
    with pytest.raises(gemini_enrich.GeminiError, match="rate_limited"):
        gemini_enrich.call_gemini("p", "k", client=m)


def test_call_gemini_raises_on_generic_http_error():
    m = _MockClient({"error": {"code": 500}}, status=500)
    with pytest.raises(gemini_enrich.GeminiError, match="http_500"):
        gemini_enrich.call_gemini("p", "k", client=m)


def test_call_gemini_raises_on_missing_content_parts():
    """The Gemini 3.x MAX_TOKENS behavior — response with no parts."""
    m = _MockClient({"candidates": [{"content": {}, "finishReason": "MAX_TOKENS"}]})
    with pytest.raises(gemini_enrich.GeminiError, match="unexpected_response"):
        gemini_enrich.call_gemini("p", "k", client=m)


# ---------------------------------------------------------------------------
# Persistence + selection
# ---------------------------------------------------------------------------

def _mk_cap(conn, normalized_key: str, metadata: dict) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind, metadata) "
            "VALUES (%s, %s, 'source', 'library', 'library', %s::jsonb) "
            "RETURNING id::text",
            (normalized_key, normalized_key.split(":", 1)[1], json.dumps(metadata)),
        )
        return cur.fetchone()[0]


def test_candidates_returns_only_rows_missing_gemini_description(conn):
    a = _mk_cap(conn, "pypi:without", {"description": "raw"})
    b = _mk_cap(conn, "pypi:withalready", {"description": "raw",
                                             "gemini_description": "already done"})
    conn.commit()

    rows = gemini_enrich._candidates(conn, kind=None, limit=100)
    keys = [r["normalized_key"] for r in rows]
    assert "pypi:without" in keys
    assert "pypi:withalready" not in keys


def test_candidates_can_filter_by_kind(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES ('source:github:foo/lib', 'lib', 'source', 'library', 'repo')"
        )
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES ('pypi:otherlib', 'otherlib', 'pypi', 'library', 'library')"
        )
    conn.commit()

    repos = gemini_enrich._candidates(conn, kind="repo", limit=100)
    assert [r["normalized_key"] for r in repos] == ["source:github:foo/lib"]


def test_write_enrichment_adds_fields_to_metadata(conn):
    cid = _mk_cap(conn, "pypi:target", {"description": "start"})
    conn.commit()

    gemini_enrich._write_enrichment(conn, cid, "does the thing.", "abc123hash")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM capability WHERE id::text = %s", (cid,))
        meta = cur.fetchone()[0]
    assert meta["description"] == "start"     # existing preserved
    assert meta["gemini_description"] == "does the thing."
    assert meta["gemini_prompt_hash"] == "abc123hash"
    assert meta["gemini_model"] == gemini_enrich.GEMINI_MODEL
