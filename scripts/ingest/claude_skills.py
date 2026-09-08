"""
Claude skill scanner.

Walks the local filesystem for SKILL.md files under:
  ~/.claude/skills/                  (user-personal skills)
  ~/.claude/plugins/marketplaces/<marketplace>/plugins/<plugin>/skills/<skill>/SKILL.md
  ~/.claude/plugins/marketplaces/<marketplace>/external_plugins/<plugin>/skills/<skill>/SKILL.md

Each SKILL.md is parsed for its YAML frontmatter (name, description,
license). The file's parent plugin name is used to build a namespaced
normalized_key: `skill:<plugin>/<skill>`.

Idempotent: content hash is stored in metadata. Re-runs on unchanged
files report 'unchanged'. Edited skills report 'updated'. Removed
skills are NOT auto-deprecated here — a two-scan absence rule belongs
in Phase 3 (judgment) once we can reason about "did the user
uninstall a plugin or just not scan its marketplace this run?"

Usage:
    python -m scripts.ingest.claude_skills
    python -m scripts.ingest.claude_skills --claude-dir /path/to/.claude

Local-only. No network calls.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from db.connection import connect
from scripts.ingest import _base

SOURCE_NAME = "claude_skills"


@dataclass
class SkillEntry:
    plugin: str                   # e.g. 'frontend-design'
    skill_name: str               # e.g. 'frontend-design' (matches SKILL.md frontmatter `name`)
    description: str
    license: Optional[str]
    file_path: Path
    content_hash: str
    body_lines: int


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    Minimal YAML frontmatter parser — good enough for the flat key: value
    structure SKILL.md uses. Multi-line values are joined on newline.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    yaml_block, body = m.group(1), m.group(2)
    fields: dict[str, str] = {}
    current_key: Optional[str] = None
    for line in yaml_block.splitlines():
        # New key on a non-indented line with a colon.
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if km:
            current_key = km.group(1)
            fields[current_key] = km.group(2).strip()
        elif current_key and (line.startswith(" ") or line.startswith("\t")):
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()
    return fields, body


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _plugin_name_from_path(skill_md_path: Path) -> str:
    """
    A SKILL.md lives at .../plugins/<plugin>/skills/<skill>/SKILL.md or
    .../external_plugins/<plugin>/skills/<skill>/SKILL.md. The plugin
    name is four levels up from SKILL.md.
    """
    parts = skill_md_path.parts
    try:
        idx = parts.index("skills")
        # The plugin dir is two before 'skills'.
        return parts[idx - 1]
    except (ValueError, IndexError):
        return "unknown"


def parse_skill_file(path: Path) -> Optional[SkillEntry]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fields, body = _parse_frontmatter(text)
    if not fields.get("name"):
        return None
    return SkillEntry(
        plugin=_plugin_name_from_path(path),
        skill_name=fields["name"],
        description=fields.get("description", ""),
        license=fields.get("license") or None,
        file_path=path,
        content_hash=_content_hash(text),
        body_lines=len(body.splitlines()),
    )


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------

def _skill_files_in(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    # rglob to catch nested skills. SKILL.md is the canonical filename;
    # some catalogs also use lowercase.
    seen: set[Path] = set()
    for pattern in ("SKILL.md", "skill.md"):
        for p in root.rglob(pattern):
            if p in seen:
                continue
            seen.add(p)
            yield p


def scan(claude_dir: Path) -> list[SkillEntry]:
    """Walk every known skill root under `claude_dir` and return parsed entries."""
    roots = [
        claude_dir / "skills",
        claude_dir / "plugins" / "marketplaces",
    ]
    entries: list[SkillEntry] = []
    seen_keys: set[tuple[str, str]] = set()
    for root in roots:
        for path in _skill_files_in(root):
            entry = parse_skill_file(path)
            if entry is None:
                continue
            key = (entry.plugin, entry.skill_name)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_one(conn, e: SkillEntry) -> str:
    normalized_key = f"skill:{e.plugin}/{e.skill_name}"
    metadata = {
        "plugin": e.plugin,
        "skill_name": e.skill_name,
        "file_path": str(e.file_path),
        "content_hash": e.content_hash,
        "body_lines": e.body_lines,
        "description": e.description,
    }
    display_name = f"{e.plugin}:{e.skill_name}"
    _cap_id, was_new, was_updated = _base.upsert_component(
        conn,
        normalized_key=normalized_key,
        display_name=display_name,
        ecosystem="source",
        capability_kind="service",
        component_kind="skill",
        runtime="claude_skill",
        cost_tier="free",
        license_spdx=e.license,
        metadata=metadata,
    )
    if was_new:
        return "new"
    if was_updated:
        return "updated"
    return "unchanged"


def run_ingest(claude_dir: Optional[Path] = None) -> dict[str, Any]:
    claude_dir = claude_dir or Path(os.path.expanduser("~/.claude"))
    entries = scan(claude_dir)
    print(f"({len(entries)} skill(s) found under {claude_dir})")

    conn = connect()
    try:
        metadata = {"claude_dir": str(claude_dir), "skill_count": len(entries)}
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            for e in entries:
                try:
                    outcome = ingest_one(conn, e)
                    if outcome == "new":         counts.new += 1
                    elif outcome == "updated":     counts.updated += 1
                    elif outcome == "unchanged":   counts.unchanged += 1
                    else:                            counts.errors += 1
                    print(f"  {outcome:10s}  skill:{e.plugin}/{e.skill_name}")
                    conn.commit()
                except Exception as ex:
                    counts.errors += 1
                    conn.rollback()
                    print(f"  error       skill:{e.plugin}/{e.skill_name}: {ex}",
                          file=sys.stderr)
            metadata["cursor"] = None
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-dir", default=None,
                    help="Override the .claude root (default: ~/.claude).")
    args = ap.parse_args()
    claude_dir = Path(os.path.expanduser(args.claude_dir)) if args.claude_dir else None
    run_ingest(claude_dir=claude_dir)


if __name__ == "__main__":
    main()
