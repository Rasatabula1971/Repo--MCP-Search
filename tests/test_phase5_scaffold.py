"""
Phase 5c done-when:

  "scaffold(proposal, out_dir) writes pipeline.yaml, README.md,
   .env.example, and stages/<n>_<slug>/{README.md, TODO.md} for every
   stage. Env vars are auto-detected from picks (openai -> OPENAI_API_
   KEY, github runtime, etc.). Stages with no pick still get a folder
   with a 'no pick' note."
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.compose import scaffold_pipeline as sc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pick(key="pypi:foo", runtime="python_import", **overrides):
    p = {
        "id": "00000000-0000-0000-0000-000000000001",
        "normalized_key": key,
        "display_name": key,
        "runtime": runtime,
        "component_kind": "library",
        "cost_tier": "free",
        "license_spdx": "MIT",
        "total_score": 0.85,
    }
    p.update(overrides)
    return p


def _proposal(**overrides):
    p = {
        "name": "test-pipeline",
        "intent": "do a small thing end to end",
        "project_id": None,
        "stages": [
            {
                "index": 0, "name": "stage-one", "role": "producer",
                "purpose": "starts the work",
                "preferred_component_kind": "library",
                "search_terms": ["kick-off"],
                "pick": _pick("pypi:starter"),
                "candidates": [_pick("pypi:starter"),
                                _pick("pypi:alt-starter")],
            },
            {
                "index": 1, "name": "stage-two", "role": "sink",
                "purpose": "finishes it",
                "preferred_component_kind": "library",
                "search_terms": ["done"],
                "pick": _pick("pypi:finisher"),
                "candidates": [_pick("pypi:finisher")],
            },
        ],
        "edges": [
            {"from_stage": 0, "to_stage": 1,
             "verdict": "compatible", "reason": "same_family",
             "detail": "python_import -> python_import", "adapter_hint": ""},
        ],
        "notes": [],
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# _slug
# ---------------------------------------------------------------------------

def test_slug_basic():
    assert sc._slug("hello world") == "hello-world"


def test_slug_strips_leading_trailing_and_collapses():
    assert sc._slug("  --Hello --World--  ") == "hello-world"


def test_slug_returns_fallback_when_empty():
    assert sc._slug("   ") == "stage"
    assert sc._slug("---") == "stage"


# ---------------------------------------------------------------------------
# _env_vars_for
# ---------------------------------------------------------------------------

def test_env_vars_detected_from_normalized_key_tokens():
    p = _proposal()
    p["stages"][0]["pick"] = _pick("pypi:openai-whisper")
    p["stages"][1]["pick"] = _pick("source:github:acme/sentry-sdk-wrapper")
    got = sc._env_vars_for(p)
    assert "OPENAI_API_KEY" in got
    assert "SENTRY_DSN" in got


def test_env_vars_detected_from_runtime_hints():
    p = _proposal()
    p["stages"][0]["pick"] = _pick("pypi:remote-x", runtime="http_endpoint")
    got = sc._env_vars_for(p)
    assert "API_BASE_URL" in got
    assert "API_KEY" in got


def test_env_vars_empty_when_no_hints_match():
    got = sc._env_vars_for(_proposal())
    assert got == []


# ---------------------------------------------------------------------------
# scaffold — writes the whole tree
# ---------------------------------------------------------------------------

def test_scaffold_writes_root_files_and_stage_folders(tmp_path):
    result = sc.scaffold(_proposal(), tmp_path)
    assert (tmp_path / "pipeline.yaml").exists()
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / ".env.example").exists()
    assert (tmp_path / "stages" / "00_stage-one" / "README.md").exists()
    assert (tmp_path / "stages" / "00_stage-one" / "TODO.md").exists()
    assert (tmp_path / "stages" / "01_stage-two" / "README.md").exists()
    assert (tmp_path / "stages" / "01_stage-two" / "TODO.md").exists()
    # Return summary tracks every file.
    assert len(result["files_written"]) >= 7


def test_scaffold_pipeline_yaml_is_valid_yaml_with_stages(tmp_path):
    sc.scaffold(_proposal(), tmp_path)
    obj = yaml.safe_load((tmp_path / "pipeline.yaml").read_text(encoding="utf-8"))
    assert obj["name"] == "test-pipeline"
    assert obj["intent"].startswith("do a small thing")
    assert [s["name"] for s in obj["stages"]] == ["stage-one", "stage-two"]
    assert obj["stages"][0]["component"]["normalized_key"] == "pypi:starter"


def test_scaffold_stage_readme_includes_pick_and_alternatives(tmp_path):
    sc.scaffold(_proposal(), tmp_path)
    text = (tmp_path / "stages" / "00_stage-one" / "README.md").read_text(encoding="utf-8")
    assert "pypi:starter" in text
    assert "pypi:alt-starter" in text
    assert "Runtime:" in text


def test_scaffold_stage_readme_notes_missing_pick(tmp_path):
    p = _proposal()
    p["stages"][0]["pick"] = None
    p["stages"][0]["candidates"] = []
    sc.scaffold(p, tmp_path)
    text = (tmp_path / "stages" / "00_stage-one" / "README.md").read_text(encoding="utf-8")
    assert "**(none)**" in text
    assert "gap" in text.lower()


def test_scaffold_stage_readme_includes_incoming_edge_verdict(tmp_path):
    sc.scaffold(_proposal(), tmp_path)
    text = (tmp_path / "stages" / "01_stage-two" / "README.md").read_text(encoding="utf-8")
    assert "compatible" in text
    assert "same_family" in text


def test_scaffold_env_example_contains_detected_vars(tmp_path):
    p = _proposal()
    p["stages"][0]["pick"] = _pick("pypi:openai-thing")
    sc.scaffold(p, tmp_path)
    env = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" in env


def test_scaffold_env_example_says_none_when_no_vars(tmp_path):
    sc.scaffold(_proposal(), tmp_path)
    env = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "No env vars" in env


def test_scaffold_returns_summary_dict(tmp_path):
    result = sc.scaffold(_proposal(), tmp_path)
    assert result["out_dir"] == str(tmp_path)
    assert "pipeline.yaml" in result["files_written"]
    assert isinstance(result["env_vars_detected"], list)


def test_scaffold_creates_out_dir_if_missing(tmp_path):
    target = tmp_path / "fresh-repo"
    assert not target.exists()
    sc.scaffold(_proposal(), target)
    assert (target / "pipeline.yaml").exists()


def test_scaffold_handles_special_chars_in_stage_name(tmp_path):
    p = _proposal()
    p["stages"][0]["name"] = "Stage One: **weird!**"
    sc.scaffold(p, tmp_path)
    # Whatever slug we produce, the folder exists under stages/.
    stage_folders = list((tmp_path / "stages").iterdir())
    assert len(stage_folders) == 2
    # First folder starts with 00_ prefix.
    assert any(f.name.startswith("00_") for f in stage_folders)
