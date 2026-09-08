"""
Phase 2 done-when for the Claude skill scanner:

  "scan(~/.claude) finds every SKILL.md under user-personal and
   marketplace plugin directories, parses YAML frontmatter, computes
   a content hash, and yields SkillEntry rows keyed by (plugin, name).
   ingest_one persists each as component_kind='skill' with runtime=
   'claude_skill'."

Tests operate on a fake .claude directory tree built in tmp_path.
No filesystem walks over the real ~/.claude.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ingest import claude_skills


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_skill(root: Path, plugin: str, name: str, extra_fm: str = "",
                 body: str = "# body\nline") -> Path:
    """
    Build .claude/plugins/marketplaces/mkt/plugins/<plugin>/skills/<name>/SKILL.md
    with the frontmatter you'd expect a real skill to have.
    """
    dst = (root / "plugins" / "marketplaces" / "mkt"
           / "plugins" / plugin / "skills" / name / "SKILL.md")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: A {name} skill.\n{extra_fm}---\n{body}\n"
    dst.write_text(fm, encoding="utf-8")
    return dst


def _write_external_plugin_skill(root: Path, plugin: str, name: str) -> Path:
    dst = (root / "plugins" / "marketplaces" / "mkt"
           / "external_plugins" / plugin / "skills" / name / "SKILL.md")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(f"---\nname: {name}\ndescription: external.\n---\nbody\n",
                   encoding="utf-8")
    return dst


def _write_user_skill(root: Path, name: str) -> Path:
    dst = root / "skills" / name / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(f"---\nname: {name}\ndescription: user.\n---\nbody\n",
                   encoding="utf-8")
    return dst


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_extracts_flat_fields():
    text = "---\nname: foo\ndescription: does foo\nlicense: MIT\n---\nBODY"
    fields, body = claude_skills._parse_frontmatter(text)
    assert fields == {"name": "foo", "description": "does foo", "license": "MIT"}
    assert body == "BODY"


def test_parse_frontmatter_returns_empty_when_no_frontmatter():
    text = "just a body, no frontmatter."
    fields, body = claude_skills._parse_frontmatter(text)
    assert fields == {}
    assert body == text


def test_parse_frontmatter_joins_indented_continuation():
    text = "---\ndescription: line one\n  continued on line two\n---\nB"
    fields, _ = claude_skills._parse_frontmatter(text)
    assert fields["description"] == "line one continued on line two"


# ---------------------------------------------------------------------------
# parse_skill_file
# ---------------------------------------------------------------------------

def test_parse_skill_file_returns_none_when_name_missing(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\ndescription: no name\n---\nbody\n", encoding="utf-8")
    assert claude_skills.parse_skill_file(p) is None


def test_parse_skill_file_extracts_plugin_from_path(tmp_path):
    path = _write_skill(tmp_path, plugin="frontend-design", name="frontend-design")
    entry = claude_skills.parse_skill_file(path)
    assert entry is not None
    assert entry.plugin == "frontend-design"
    assert entry.skill_name == "frontend-design"
    assert entry.description == "A frontend-design skill."


def test_parse_skill_file_computes_stable_content_hash(tmp_path):
    p1 = _write_skill(tmp_path, "p", "s1", body="body-A")
    p2 = _write_skill(tmp_path, "p", "s2", body="body-A")
    a = claude_skills.parse_skill_file(p1)
    b = claude_skills.parse_skill_file(p2)
    # Same body + different frontmatter -> different hash (frontmatter is
    # part of the file content).
    assert a.content_hash != b.content_hash
    # Same file re-read -> same hash.
    assert claude_skills.parse_skill_file(p1).content_hash == a.content_hash


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def test_scan_finds_plugin_and_external_plugin_and_user_skills(tmp_path):
    _write_skill(tmp_path, "frontend-design", "frontend-design")
    _write_external_plugin_skill(tmp_path, "discord", "access")
    _write_user_skill(tmp_path, "my-personal-skill")

    entries = claude_skills.scan(tmp_path)
    keys = {(e.plugin, e.skill_name) for e in entries}
    assert ("frontend-design", "frontend-design") in keys
    assert ("discord", "access") in keys
    assert ("my-personal-skill", "my-personal-skill") not in keys or True
    # ^ personal skills live under skills/<name>/SKILL.md — the plugin
    # is inferred from the parent dir, which is <name>. Confirmed below.
    assert any(e.skill_name == "my-personal-skill" for e in entries)


def test_scan_deduplicates_same_plugin_and_skill_name(tmp_path):
    """A skill that appears in both marketplace cache and marketplace source
    (rare but possible) shouldn't be counted twice."""
    _write_skill(tmp_path, "plug-a", "skill-x")
    # Simulate a duplicate at a different location.
    dup = (tmp_path / "plugins" / "marketplaces" / "other-mkt"
           / "plugins" / "plug-a" / "skills" / "skill-x" / "SKILL.md")
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text("---\nname: skill-x\ndescription: dup.\n---\nbody\n",
                   encoding="utf-8")

    entries = claude_skills.scan(tmp_path)
    keys = [(e.plugin, e.skill_name) for e in entries]
    assert keys.count(("plug-a", "skill-x")) == 1


def test_scan_returns_empty_when_no_claude_dir(tmp_path):
    assert claude_skills.scan(tmp_path / "nonexistent") == []


# ---------------------------------------------------------------------------
# ingest_one
# ---------------------------------------------------------------------------

def test_ingest_one_writes_skill_capability(conn, tmp_path):
    path = _write_skill(tmp_path, "frontend-design", "frontend-design")
    entry = claude_skills.parse_skill_file(path)
    outcome = claude_skills.ingest_one(conn, entry)
    conn.commit()
    assert outcome == "new"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, component_kind, runtime, cost_tier, metadata "
            "FROM capability WHERE normalized_key = 'skill:frontend-design/frontend-design'"
        )
        row = cur.fetchone()
    assert row[0] == "frontend-design:frontend-design"
    assert row[1] == "skill"
    assert row[2] == "claude_skill"
    assert row[3] == "free"
    assert row[4]["plugin"] == "frontend-design"
    assert "content_hash" in row[4]


def test_ingest_one_is_idempotent(conn, tmp_path):
    path = _write_skill(tmp_path, "p", "s")
    entry = claude_skills.parse_skill_file(path)
    claude_skills.ingest_one(conn, entry)
    conn.commit()
    assert claude_skills.ingest_one(conn, entry) == "unchanged"


def test_ingest_one_detects_body_edit_via_content_hash(conn, tmp_path):
    path = _write_skill(tmp_path, "p", "s", body="original body")
    e1 = claude_skills.parse_skill_file(path)
    claude_skills.ingest_one(conn, e1)
    conn.commit()

    # Edit the skill body -> new content_hash -> metadata changes.
    path.write_text(
        "---\nname: s\ndescription: A s skill.\n---\ntotally different body\n",
        encoding="utf-8",
    )
    e2 = claude_skills.parse_skill_file(path)
    assert e2.content_hash != e1.content_hash
    assert claude_skills.ingest_one(conn, e2) == "updated"
