"""
Phase 3c done-when:

  "Every deprecation/supersession decision is recorded in component_
   classification with prompt_name='supersession_detection'. Deprecated
   + high confidence patches metadata.deprecated_reason. If the
   named successor resolves to a registry row, a capability_link of
   kind 'superseded_by' is also written."
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from scripts.judge import detect_supersession as ds


# ---------------------------------------------------------------------------
# _successor_candidates
# ---------------------------------------------------------------------------

def test_successor_from_github_url():
    keys = ds._successor_candidates("https://github.com/psf/requests")
    assert "source:github:psf/requests" in keys


def test_successor_from_owner_slash_name():
    keys = ds._successor_candidates("psf/requests")
    assert "source:github:psf/requests" in keys


def test_successor_from_bare_token_tries_pypi_and_npm():
    keys = ds._successor_candidates("httpx")
    assert "pypi:httpx" in keys
    assert "npm:httpx" in keys


def test_successor_lowercases_owner_and_name():
    keys = ds._successor_candidates("PSF/Requests")
    assert "source:github:psf/requests" in keys


def test_successor_strips_git_suffix():
    keys = ds._successor_candidates("https://github.com/psf/requests.git")
    assert "source:github:psf/requests" in keys


def test_successor_empty_string_returns_empty():
    assert ds._successor_candidates("") == []
    assert ds._successor_candidates(None) == []


# ---------------------------------------------------------------------------
# _resolve_successor against a real DB
# ---------------------------------------------------------------------------

def _mk(conn, key, kind="repo"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, component_kind) "
            "VALUES (%s, %s, 'source', 'library', %s) RETURNING id::text",
            (key, key, kind),
        )
        return cur.fetchone()[0]


def test_resolve_finds_matching_registry_row(conn):
    target = _mk(conn, "source:github:psf/requests")
    conn.commit()
    assert ds._resolve_successor(conn, "psf/requests") == target


def test_resolve_returns_none_when_no_match(conn):
    assert ds._resolve_successor(conn, "some-unknown-package") is None


def test_resolve_returns_none_for_none_input(conn):
    assert ds._resolve_successor(conn, None) is None


# ---------------------------------------------------------------------------
# parse_decision
# ---------------------------------------------------------------------------

def _dec(**overrides):
    obj = {"deprecated": False, "confidence": 0.9,
           "successor_name": None, "rationale": "not deprecated"}
    obj.update(overrides)
    return json.dumps(obj)


def test_parse_valid_not_deprecated():
    d = ds.parse_decision(_dec())
    assert d.deprecated is False
    assert d.successor_name is None


def test_parse_valid_deprecated_with_successor():
    d = ds.parse_decision(_dec(deprecated=True, confidence=0.9,
                                 successor_name="psf/requests",
                                 rationale="says archived, use requests"))
    assert d.deprecated is True
    assert d.successor_name == "psf/requests"


def test_parse_strips_empty_successor_to_none():
    d = ds.parse_decision(_dec(successor_name="   "))
    assert d.successor_name is None


def test_parse_rejects_non_bool_deprecated():
    with pytest.raises(ds.MalformedResponse, match="bad_deprecated"):
        ds.parse_decision(_dec(deprecated="maybe"))


def test_parse_rejects_non_string_successor():
    with pytest.raises(ds.MalformedResponse, match="bad_successor_name"):
        ds.parse_decision(_dec(successor_name=42))


def test_parse_rejects_confidence_out_of_range():
    with pytest.raises(ds.MalformedResponse, match="confidence_out_of_range"):
        ds.parse_decision(_dec(confidence=2.0))


# ---------------------------------------------------------------------------
# _record — the whole flow
# ---------------------------------------------------------------------------

def _metadata(conn, cap_id):
    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM capability WHERE id::text = %s", (cap_id,))
        return cur.fetchone()[0]


def _link_rows(conn, cap_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT link_kind, target_capability_id::text, confidence "
            "FROM capability_link WHERE source_capability_id::text = %s",
            (cap_id,),
        )
        return cur.fetchall()


def _classification_count(conn, cap_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM component_classification "
            "WHERE capability_id::text = %s AND prompt_name = %s",
            (cap_id, ds.PROMPT_NAME),
        )
        return cur.fetchone()[0]


def test_record_not_deprecated_only_writes_audit(conn):
    cid = _mk(conn, "source:github:acme/live")
    cap = {"id": cid, "normalized_key": "source:github:acme/live",
           "component_kind": "repo", "kind": "library"}
    conn.commit()

    outcome = ds._record(conn, cap,
                          ds.SupersessionDecision(False, 0.9, None, "active"),
                          "h", None)
    conn.commit()
    assert outcome == "not_deprecated"
    assert _metadata(conn, cid).get("deprecated_reason") is None
    assert _link_rows(conn, cid) == []
    assert _classification_count(conn, cid) == 1


def test_record_deprecated_high_conf_no_successor_marks_metadata_only(conn):
    cid = _mk(conn, "source:github:acme/dead")
    cap = {"id": cid, "normalized_key": "source:github:acme/dead",
           "component_kind": "repo", "kind": "library"}
    conn.commit()

    outcome = ds._record(conn, cap,
                          ds.SupersessionDecision(True, 0.9, None, "archived"),
                          "h", None)
    conn.commit()
    assert outcome == "marked_deprecated"
    meta = _metadata(conn, cid)
    assert meta["deprecated_reason"] == "supersession_detected"
    assert _link_rows(conn, cid) == []


def test_record_deprecated_with_resolved_successor_writes_link(conn):
    old_id = _mk(conn, "source:github:acme/old")
    new_id = _mk(conn, "source:github:acme/new")
    cap = {"id": old_id, "normalized_key": "source:github:acme/old",
           "component_kind": "repo", "kind": "library"}
    conn.commit()

    outcome = ds._record(conn, cap,
                          ds.SupersessionDecision(True, 0.9, "acme/new",
                                                    "moved to acme/new"),
                          "h", new_id)
    conn.commit()
    assert outcome == "linked_successor"
    links = _link_rows(conn, old_id)
    assert len(links) == 1
    assert links[0][0] == "superseded_by"
    assert links[0][1] == new_id
    assert _metadata(conn, old_id)["deprecated_reason"] == "supersession_detected"


def test_record_deprecated_low_confidence_is_no_action(conn):
    cid = _mk(conn, "source:github:acme/maybe")
    cap = {"id": cid, "normalized_key": "source:github:acme/maybe",
           "component_kind": "repo", "kind": "library"}
    conn.commit()

    outcome = ds._record(conn, cap,
                          ds.SupersessionDecision(True, 0.4, "acme/new", "unsure"),
                          "h", None)
    conn.commit()
    assert outcome == "no_action"
    assert _metadata(conn, cid).get("deprecated_reason") is None
    assert _classification_count(conn, cid) == 1


# ---------------------------------------------------------------------------
# _candidates — sanity check the filter query
# ---------------------------------------------------------------------------

def test_candidates_skips_already_asked_at_current_prompt_version(conn):
    cid = _mk(conn, "source:github:acme/thing")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " confidence, applied, prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, 'repo', 'library', 0.9, false, %s, %s, 'h', 'm')",
            (cid, ds.PROMPT_NAME, ds.PROMPT_VERSION),
        )
    conn.commit()

    rows = ds._candidates(conn, kind=None, limit=100, force=False)
    assert cid not in [r["id"] for r in rows]
