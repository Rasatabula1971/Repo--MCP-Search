"""
Capability key normalization — Step 8's design call.

Rule: two repos that publish the same package should share a
capability. Two repos with no published identity get their own.

Algorithm:
  1. Look at manifest evidence for a project name + ecosystem.
     - pyproject.toml [project].name        → 'pypi:{name}'
     - package.json  .name                  → 'npm:{name}'
  2. Fall back to source identity            → 'source:{provider}:{external_key}'

The lookup is DETERMINISTIC — no LLM in the loop. Feeding the same
evidence in produces the same normalized_key every time. That's what
makes 'the same capability' a real equivalence class rather than a
best-effort guess.

Names are lowercased before keying. PyPI is officially case-insensitive
per PEP 503; npm has treated names as case-insensitive since 2017.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedIdentity:
    normalized_key: str
    display_name: str
    ecosystem: str | None       # 'pypi', 'npm', 'source', None
    kind: str                   # 'library', 'unknown' — refined later


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def normalize_from_files(
    *,
    provider_name: str,
    external_key: str,
    files: dict[str, bytes],
) -> NormalizedIdentity:
    """
    Determine the capability identity for a set of source files.

    files is a small mapping of {path: content} — we only need the
    manifest files, not the whole snapshot. Callers can pre-filter.
    """
    # Try pyproject.toml first (PEP 621 is now standard).
    for path in _find_files(files, "pyproject.toml"):
        ident = _from_pyproject(files[path])
        if ident is not None:
            return ident

    # Then package.json.
    for path in _find_files(files, "package.json"):
        ident = _from_package_json(files[path])
        if ident is not None:
            return ident

    # Fallback: no published identity.
    return NormalizedIdentity(
        normalized_key=f"source:{provider_name}:{external_key.lower()}",
        display_name=external_key,
        ecosystem="source",
        kind="unknown",
    )


# ---------------------------------------------------------------------------
# Manifest parsers
# ---------------------------------------------------------------------------

def _from_pyproject(content: bytes) -> NormalizedIdentity | None:
    try:
        data = tomllib.loads(content.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return None
    project = (data or {}).get("project") or {}
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    canonical = _canonical_pypi(name)
    return NormalizedIdentity(
        normalized_key=f"pypi:{canonical}",
        display_name=name.strip(),
        ecosystem="pypi",
        kind="library",
    )


def _from_package_json(content: bytes) -> NormalizedIdentity | None:
    try:
        data = json.loads(content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    canonical = name.strip().lower()
    return NormalizedIdentity(
        normalized_key=f"npm:{canonical}",
        display_name=name.strip(),
        ecosystem="npm",
        kind="library",
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _find_files(files: dict[str, bytes], basename: str) -> list[str]:
    """Return paths whose basename matches. Repo-root files come first
    so a top-level pyproject.toml wins over one nested in examples/."""
    hits = [p for p in files if p == basename or p.endswith("/" + basename)]
    hits.sort(key=lambda p: (p.count("/"), p))
    return hits


# PEP 503 canonicalization: lowercase, and runs of [_.-] normalize to '-'.
_PYPI_NORMALIZE_RE = re.compile(r"[-_.]+")


def _canonical_pypi(name: str) -> str:
    return _PYPI_NORMALIZE_RE.sub("-", name.strip().lower())
