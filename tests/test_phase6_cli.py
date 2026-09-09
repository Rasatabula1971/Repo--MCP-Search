"""
Phase 6 done-when:

  "cip-composer CLI's parser resolves every subcommand to the right
   handler, format helpers render sensibly, and the entry-point module
   is importable."
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from cip_composer import cli


# ---------------------------------------------------------------------------
# _build_parser — every subcommand wired correctly
# ---------------------------------------------------------------------------

def test_parser_wires_find_command():
    p = cli._build_parser()
    ns = p.parse_args(["find", "video"])
    assert ns.func is cli.cmd_find
    assert ns.query == "video"


def test_parser_wires_browse_command():
    p = cli._build_parser()
    ns = p.parse_args(["browse", "--kind", "repo"])
    assert ns.func is cli.cmd_browse
    assert ns.kind == "repo"


def test_parser_wires_suggest_command():
    p = cli._build_parser()
    ns = p.parse_args(["suggest", "an intent", "--max-stages", "4"])
    assert ns.func is cli.cmd_suggest
    assert ns.intent == "an intent"
    assert ns.max_stages == 4


def test_parser_wires_scaffold_command(tmp_path):
    p = cli._build_parser()
    ns = p.parse_args(["scaffold",
                        "--proposal", "p.json", "--out-dir", str(tmp_path)])
    assert ns.func is cli.cmd_scaffold
    assert ns.proposal == "p.json"
    assert ns.out_dir == str(tmp_path)


def test_parser_wires_flow_command(tmp_path):
    p = cli._build_parser()
    ns = p.parse_args(["flow", "intent", "--out-dir", str(tmp_path)])
    assert ns.func is cli.cmd_flow
    assert ns.intent == "intent"


def test_parser_wires_info_command():
    p = cli._build_parser()
    ns = p.parse_args(["info"])
    assert ns.func is cli.cmd_info


def test_parser_requires_subcommand():
    p = cli._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_parser_rejects_unknown_subcommand():
    p = cli._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["nonsense"])


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def test_print_row_table_handles_empty():
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_row_table([], ["a", "b"])
    assert "(no rows)" in buf.getvalue()


def test_print_row_table_renders_with_columns():
    rows = [{"a": "one", "b": 1}, {"a": "two", "b": 2}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_row_table(rows, ["a", "b"])
    out = buf.getvalue()
    assert "one" in out and "two" in out
    assert "1" in out and "2" in out


def test_print_row_table_tolerates_missing_field():
    rows = [{"a": "one"}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_row_table(rows, ["a", "b"])
    out = buf.getvalue()
    assert "one" in out


def test_print_json_pretty_prints():
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_json({"a": 1})
    parsed = json.loads(buf.getvalue())
    assert parsed == {"a": 1}


# ---------------------------------------------------------------------------
# cmd_scaffold — end to end without touching the DB
# ---------------------------------------------------------------------------

def test_cmd_scaffold_writes_files(tmp_path):
    proposal = {
        "name": "t", "intent": "test", "project_id": None,
        "stages": [{
            "index": 0, "name": "s", "role": "producer", "purpose": "p",
            "preferred_component_kind": "library", "search_terms": ["s"],
            "pick": {"id": "id", "normalized_key": "pypi:x", "display_name": "x",
                      "runtime": "python_import", "component_kind": "library",
                      "cost_tier": "free", "license_spdx": "MIT"},
            "candidates": [],
        }],
        "edges": [], "notes": [],
    }
    proposal_path = tmp_path / "p.json"
    out_dir = tmp_path / "out"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

    args = SimpleNamespace(proposal=str(proposal_path), out_dir=str(out_dir))
    rc = cli.cmd_scaffold(args)
    assert rc == 0
    assert (out_dir / "pipeline.yaml").exists()
