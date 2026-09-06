"""
Manifest extractor.

Parses common dependency manifests and emits one 'dependency' evidence
item per declared dependency. Doesn't judge — just says "here's a
declared dependency at manifest_key locator".

Supports:
  - pyproject.toml (PEP 621 [project.dependencies] and
                     [project.optional-dependencies])
  - requirements*.txt
  - package.json (dependencies / devDependencies / peerDependencies)

setup.py is deliberately NOT parsed: parsing it correctly requires
executing it, which violates "fetching is not executing" — and if we
did execute, the security posture would change. We do emit a low-signal
'setup_py_detected' file_range item so downstream knows to look manually.
"""
from __future__ import annotations

import json
import re
import tomllib
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    LOCATOR_MANIFEST_KEY,
    EvidenceItem,
    SourceFile,
)


class ManifestExtractor:
    name = "manifests"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            path = f.path
            if path == "pyproject.toml" or path.endswith("/pyproject.toml"):
                yield from self._pyproject(f)
            elif _is_requirements_txt(path):
                yield from self._requirements_txt(f)
            elif path == "package.json" or path.endswith("/package.json"):
                yield from self._package_json(f)
            elif path == "setup.py" or path.endswith("/setup.py"):
                yield from self._setup_py(f)

    # ------------------------------------------------------------------
    # pyproject.toml (PEP 621)
    # ------------------------------------------------------------------

    def _pyproject(self, f: SourceFile) -> Iterator[EvidenceItem]:
        try:
            data = tomllib.loads(f.content.decode("utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            return  # malformed; skip. Not our job to interpret.

        project = data.get("project", {}) or {}
        # Runtime deps
        for spec in project.get("dependencies", []) or []:
            name, version_spec = _split_pep508(spec)
            yield EvidenceItem(
                evidence_type="dependency",
                locator_kind=LOCATOR_MANIFEST_KEY,
                locator={
                    "path": f.path,
                    "key_path": ["project", "dependencies"],
                },
                extracted_value={
                    "name": name,
                    "version_spec": version_spec,
                    "kind": "runtime",
                    "ecosystem": "pypi",
                    "raw": spec,
                },
            )
        # Optional deps (dev, test, docs, etc.)
        for group, specs in (project.get("optional-dependencies") or {}).items():
            for spec in specs or []:
                name, version_spec = _split_pep508(spec)
                yield EvidenceItem(
                    evidence_type="dependency",
                    locator_kind=LOCATOR_MANIFEST_KEY,
                    locator={
                        "path": f.path,
                        "key_path": ["project", "optional-dependencies", group],
                    },
                    extracted_value={
                        "name": name,
                        "version_spec": version_spec,
                        "kind": f"optional:{group}",
                        "ecosystem": "pypi",
                        "raw": spec,
                    },
                )

    # ------------------------------------------------------------------
    # requirements*.txt
    # ------------------------------------------------------------------

    def _requirements_txt(self, f: SourceFile) -> Iterator[EvidenceItem]:
        text = f.content.decode("utf-8", errors="replace")
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                # Skip blanks, comments, -r/-e/-c option lines.
                continue
            name, version_spec = _split_pep508(line)
            yield EvidenceItem(
                evidence_type="dependency",
                locator_kind=LOCATOR_FILE_RANGE,
                locator={
                    "path": f.path,
                    "start_line": lineno,
                    "end_line": lineno,
                },
                extracted_value={
                    "name": name,
                    "version_spec": version_spec,
                    "kind": "runtime",
                    "ecosystem": "pypi",
                    "raw": line,
                },
            )

    # ------------------------------------------------------------------
    # package.json
    # ------------------------------------------------------------------

    def _package_json(self, f: SourceFile) -> Iterator[EvidenceItem]:
        try:
            data = json.loads(f.content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        for kind, npm_key in [
            ("runtime", "dependencies"),
            ("dev", "devDependencies"),
            ("peer", "peerDependencies"),
        ]:
            deps = data.get(npm_key) or {}
            if not isinstance(deps, dict):
                continue
            for name, version_spec in deps.items():
                yield EvidenceItem(
                    evidence_type="dependency",
                    locator_kind=LOCATOR_MANIFEST_KEY,
                    locator={
                        "path": f.path,
                        "key_path": [npm_key, name],
                    },
                    extracted_value={
                        "name": name,
                        "version_spec": str(version_spec),
                        "kind": kind,
                        "ecosystem": "npm",
                    },
                )

    # ------------------------------------------------------------------
    # setup.py — do not execute; just flag its presence for manual review
    # ------------------------------------------------------------------

    def _setup_py(self, f: SourceFile) -> Iterator[EvidenceItem]:
        # Line count for a proper file_range locator.
        line_count = f.content.count(b"\n") + 1
        yield EvidenceItem(
            evidence_type="setup_py_detected",
            locator_kind=LOCATOR_FILE_RANGE,
            locator={
                "path": f.path,
                "start_line": 1,
                "end_line": max(line_count, 1),
            },
            extracted_value={
                "note": (
                    "setup.py present but not parsed; dependencies "
                    "declared here are opaque without executing it"
                ),
            },
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# PEP 508: name is [A-Za-z0-9][A-Za-z0-9._-]*, then optional extras
# in brackets, then a version specifier. We just want name and the rest.
_PEP508_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _split_pep508(spec: str) -> tuple[str, str]:
    """
    'httpx>=0.27,<1.0' -> ('httpx', '>=0.27,<1.0')
    'requests' -> ('requests', '')
    'flask[async]>=2' -> ('flask', '[async]>=2')
    """
    spec = spec.strip()
    m = _PEP508_NAME_RE.match(spec)
    if not m:
        return spec, ""
    name = m.group(1)
    rest = spec[len(name):].strip()
    return name, rest


_REQUIREMENTS_RE = re.compile(
    r"(^|/)requirements([-_].+)?\.txt$", re.IGNORECASE
)


def _is_requirements_txt(path: str) -> bool:
    return bool(_REQUIREMENTS_RE.search(path))
