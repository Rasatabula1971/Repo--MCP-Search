"""
Phase 5a done-when:

  "check_pair returns 'compatible' for same runtime family with matching
   I/O, 'adapter_needed' for bridgeable runtime pairs OR same-family with
   mismatched I/O types, 'incompatible' when no bridge exists. Missing
   I/O types degrade gracefully; missing runtimes surface as adapter_
   needed with reason='unknown_runtime'."
"""
from __future__ import annotations

import uuid

import pytest

from core.policy import compatibility as cp
from mcp_server import queries as q


# ---------------------------------------------------------------------------
# check_pair — pure
# ---------------------------------------------------------------------------

def test_same_runtime_family_compatible():
    v = cp.check_pair({"runtime": "python_import"}, {"runtime": "python_import"})
    assert v.verdict == "compatible"


def test_mcp_stdio_and_sse_are_same_family():
    v = cp.check_pair({"runtime": "mcp_stdio"}, {"runtime": "mcp_sse"})
    assert v.verdict == "compatible"


def test_python_and_cli_are_bridgeable():
    v = cp.check_pair({"runtime": "python_import"}, {"runtime": "cli_subprocess"})
    assert v.verdict == "adapter_needed"
    assert "subprocess.run" in v.adapter_hint


def test_git_clone_to_python_import_is_bridgeable():
    v = cp.check_pair({"runtime": "git_clone"}, {"runtime": "python_import"})
    assert v.verdict == "adapter_needed"


def test_claude_skill_to_mcp_stdio_is_bridgeable():
    v = cp.check_pair({"runtime": "claude_skill"}, {"runtime": "mcp_stdio"})
    assert v.verdict == "adapter_needed"


def test_unrelated_runtimes_incompatible():
    v = cp.check_pair({"runtime": "cargo_import"}, {"runtime": "claude_skill"})
    assert v.verdict == "incompatible"
    assert v.reason == "no_known_bridge"


def test_missing_runtime_degrades_to_adapter_needed():
    v = cp.check_pair({"runtime": ""}, {"runtime": "python_import"})
    assert v.verdict == "adapter_needed"
    assert v.reason == "unknown_runtime"


# ---------------------------------------------------------------------------
# I/O type check
# ---------------------------------------------------------------------------

def test_matching_io_types_stay_compatible():
    v = cp.check_pair(
        {"runtime": "python_import"}, {"runtime": "python_import"},
        source_output_type={"kind": "list", "of": "Tag"},
        target_input_type={"kind": "list", "of": "Tag"},
    )
    assert v.verdict == "compatible"
    assert v.io_type_check == "match"


def test_mismatched_io_within_same_family_downgrades_to_adapter():
    v = cp.check_pair(
        {"runtime": "python_import"}, {"runtime": "python_import"},
        source_output_type={"kind": "list"},
        target_input_type={"kind": "object"},
    )
    assert v.verdict == "adapter_needed"
    assert v.reason == "io_mismatch_within_family"
    assert v.io_type_check == "mismatch"


def test_missing_io_types_report_unknown_not_mismatch():
    v = cp.check_pair(
        {"runtime": "python_import"}, {"runtime": "python_import"},
        source_output_type=None, target_input_type=None,
    )
    assert v.verdict == "compatible"
    assert v.io_type_check == "unknown_io"


def test_one_side_missing_io_is_unknown():
    v = cp.check_pair(
        {"runtime": "python_import"}, {"runtime": "python_import"},
        source_output_type={"kind": "list"}, target_input_type=None,
    )
    assert v.io_type_check == "unknown_io"


def test_matching_kinds_with_different_type_field_is_mismatch():
    v = cp.check_pair(
        {"runtime": "python_import"}, {"runtime": "python_import"},
        source_output_type={"kind": "primitive", "type": "str"},
        target_input_type={"kind": "primitive", "type": "int"},
    )
    assert v.io_type_check == "mismatch"


# ---------------------------------------------------------------------------
# Query-level integration
# ---------------------------------------------------------------------------

def _mk(conn, key, runtime, component_kind="library"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, "
            " component_kind, runtime) "
            "VALUES (%s, %s, 'source', 'library', %s, %s) RETURNING id::text",
            (key, key, component_kind, runtime),
        )
        return cur.fetchone()[0]


def test_query_compat_returns_none_for_bad_ids(conn):
    assert q.capability_compatibility(conn, "not-a-uuid", "also-bad") is None


def test_query_compat_returns_none_for_missing_capability(conn):
    a = _mk(conn, "pypi:x", "python_import")
    conn.commit()
    assert q.capability_compatibility(conn, a, str(uuid.uuid4())) is None


def test_query_compat_returns_verdict_for_real_pair(conn):
    a = _mk(conn, "pypi:src", "python_import")
    b = _mk(conn, "npm:tgt",  "http_endpoint")
    conn.commit()
    result = q.capability_compatibility(conn, a, b)
    assert result["verdict"] == "adapter_needed"
    assert result["source"]["normalized_key"] == "pypi:src"
    assert result["target"]["normalized_key"] == "npm:tgt"
    assert "httpx" in result["adapter_hint"]
