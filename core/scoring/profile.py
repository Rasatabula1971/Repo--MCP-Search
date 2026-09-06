"""
Scoring profile — the rubric, as data.

A profile is loaded from YAML, validated, and canonicalized before being
hashed. The hash is what makes 'same profile' verifiable across runs
and machines. Two edits that result in the same canonical form produce
the same hash; any material change produces a different one.

This module deliberately has no runtime dependency on core.judgment or
any provider client. The import-linter contract enforces it; the code
here doesn't need those imports for what it does.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Dimension kinds recognised by the scorer. Adding a new kind requires
# code (in scorer.py) — the profile can't express arbitrary logic on its
# own, which is the point: no eval, no code-in-config surprises.
DIMENSION_KINDS = frozenset({"presence", "count", "absence"})


@dataclass(frozen=True)
class Dimension:
    name: str
    weight: float                    # 0..1
    kind: str                        # one of DIMENSION_KINDS
    evidence_type: str               # which evidence rows this reads
    normalize_ceiling: int | None = None   # count kind
    normalize_cap: int | None = None       # absence kind
    require_field: str | None = None
    require_field_not_value: Any = None


@dataclass(frozen=True)
class Profile:
    name: str
    version: int
    dimensions: tuple[Dimension, ...]
    profile_hash: str                # SHA256 of canonical form
    raw: dict                        # the exact dict as loaded, for persistence

    def dimension(self, name: str) -> Dimension:
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(name)


class ProfileError(ValueError):
    """Raised when a profile YAML fails validation."""


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def load(path: str | Path) -> Profile:
    text = Path(path).read_text()
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ProfileError("top-level profile must be a mapping")
    return _from_dict(raw)


def _from_dict(raw: dict) -> Profile:
    name = raw.get("name")
    version = raw.get("version")
    dims_raw = raw.get("dimensions")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("profile 'name' must be a non-empty string")
    if not isinstance(version, int) or version < 1:
        raise ProfileError("profile 'version' must be a positive integer")
    if not isinstance(dims_raw, list) or not dims_raw:
        raise ProfileError("profile 'dimensions' must be a non-empty list")

    dims: list[Dimension] = []
    for d in dims_raw:
        dims.append(_dim_from_dict(d))

    _validate_weights_sum_to_one(dims)
    _validate_unique_names(dims)

    profile_hash = _canonical_hash(name, version, [d.__dict__ for d in dims])
    return Profile(
        name=name,
        version=version,
        dimensions=tuple(dims),
        profile_hash=profile_hash,
        raw=raw,
    )


def _dim_from_dict(d: dict) -> Dimension:
    if not isinstance(d, dict):
        raise ProfileError(f"dimension must be a mapping, got {type(d).__name__}")

    name = d.get("name")
    weight = d.get("weight")
    kind = d.get("kind")
    inputs = d.get("inputs") or {}

    if not isinstance(name, str) or not name.strip():
        raise ProfileError("dimension.name must be a non-empty string")
    if not isinstance(weight, (int, float)):
        raise ProfileError(f"dimension {name!r}: weight must be a number")
    if not (0.0 < weight <= 1.0):
        raise ProfileError(
            f"dimension {name!r}: weight {weight} out of (0, 1]"
        )
    if kind not in DIMENSION_KINDS:
        raise ProfileError(
            f"dimension {name!r}: unknown kind {kind!r}; "
            f"expected one of {sorted(DIMENSION_KINDS)}"
        )
    ev_type = inputs.get("evidence_type")
    if not isinstance(ev_type, str) or not ev_type.strip():
        raise ProfileError(
            f"dimension {name!r}: inputs.evidence_type is required"
        )

    normalize = d.get("normalize") or {}
    ceiling = normalize.get("ceiling")
    cap = normalize.get("cap")
    if kind == "count":
        if not isinstance(ceiling, int) or ceiling < 1:
            raise ProfileError(
                f"dimension {name!r}: count kind needs "
                f"normalize.ceiling >= 1"
            )
    if kind == "absence":
        if not isinstance(cap, int) or cap < 1:
            raise ProfileError(
                f"dimension {name!r}: absence kind needs "
                f"normalize.cap >= 1"
            )

    require = d.get("require_field_not") or {}
    require_field = require.get("field") if require else None
    require_field_not_value = require.get("value") if require else None
    if require and require_field is None:
        raise ProfileError(
            f"dimension {name!r}: require_field_not needs 'field'"
        )

    return Dimension(
        name=name.strip(),
        weight=float(weight),
        kind=kind,
        evidence_type=ev_type.strip(),
        normalize_ceiling=ceiling if kind == "count" else None,
        normalize_cap=cap if kind == "absence" else None,
        require_field=require_field,
        require_field_not_value=require_field_not_value,
    )


def _validate_weights_sum_to_one(dims: list[Dimension]) -> None:
    total = sum(d.weight for d in dims)
    # Tolerate float slop, but flag anything meaningfully off — a profile
    # whose weights sum to 0.95 or 1.05 is a bug, not a nuance.
    if abs(total - 1.0) > 1e-6:
        raise ProfileError(
            f"dimension weights must sum to 1.0 (got {total:.4f})"
        )


def _validate_unique_names(dims: list[Dimension]) -> None:
    seen: set[str] = set()
    for d in dims:
        if d.name in seen:
            raise ProfileError(f"duplicate dimension name: {d.name!r}")
        seen.add(d.name)


def _canonical_hash(name: str, version: int, dims_dicts: list[dict]) -> str:
    """
    Hash inputs after canonicalising. Keys sorted, numbers as their JSON
    representation. Same profile in a different YAML style → same hash.
    """
    payload = json.dumps(
        {"name": name, "version": version, "dimensions": dims_dicts},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
