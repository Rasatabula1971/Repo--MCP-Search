"""
Scorer — pure, deterministic.

Given a profile and an evidence set, produce a Scorecard. No DB, no
network, no imports from core.judgment or any provider client — the
import-linter contract makes that a hard rule; this module just
happens to be trivial to comply with because it doesn't need any of
those things.

The determinism claim:
  Same profile + same evidence set → identical Scorecard (including
  the computed_hash), byte for byte, on any machine, in any process.

This module never reads judgment_response.self_confidence. Confidence
here is a function of evidence coverage and contradiction — that's it.
Using model self-confidence would be a category error: the model's
opinion of its own opinion is not evidence about the source.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from core.scoring.profile import Dimension, Profile


# ---------------------------------------------------------------------------
# Input + output types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """
    A minimal view of an evidence_item row — everything the scorer
    needs, nothing it doesn't. Scoring never reads locators or reaches
    back to file bytes.
    """
    id: str                        # uuid as string
    evidence_type: str
    extracted_value: dict


@dataclass(frozen=True)
class DimensionResult:
    dimension_name: str
    raw_score: float
    weight: float
    weighted_score: float
    coverage: float                # 0.0 or 1.0 for MVP dimensions
    contradiction: float           # 0.0 for MVP; hook for consensus
    evidence_count: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Scorecard:
    total_score: float
    confidence: float
    computed_hash: str
    dimensions: tuple[DimensionResult, ...]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def score(profile: Profile, evidence: list[Evidence]) -> Scorecard:
    """
    Compute a scorecard. Pure function of its inputs.
    """
    # Group evidence by type once, deterministically ordered by id so
    # the eventual hash is stable regardless of caller order.
    by_type: dict[str, list[Evidence]] = {}
    for e in sorted(evidence, key=lambda x: x.id):
        by_type.setdefault(e.evidence_type, []).append(e)

    dim_results: list[DimensionResult] = []
    for dim in profile.dimensions:
        candidates = by_type.get(dim.evidence_type, [])
        matched = [e for e in candidates if _matches_filter(e, dim)]
        raw = _compute_raw_score(dim, matched)
        weighted = _round3(raw * dim.weight)
        coverage = 1.0 if matched else 0.0
        contradiction = _measure_contradiction(dim, matched)
        dim_results.append(DimensionResult(
            dimension_name=dim.name,
            raw_score=_round3(raw),
            weight=_round3(dim.weight),
            weighted_score=weighted,
            coverage=coverage,
            contradiction=contradiction,
            evidence_count=len(matched),
            evidence_ids=tuple(e.id for e in matched),
        ))

    total = _round3(sum(r.weighted_score for r in dim_results))
    confidence = _compute_confidence(dim_results)

    computed_hash = _compute_hash(profile, dim_results)

    return Scorecard(
        total_score=total,
        confidence=confidence,
        computed_hash=computed_hash,
        dimensions=tuple(dim_results),
    )


# ---------------------------------------------------------------------------
# Per-dimension calculation
# ---------------------------------------------------------------------------

def _compute_raw_score(dim: Dimension, matched: list[Evidence]) -> float:
    if dim.kind == "presence":
        return 1.0 if matched else 0.0
    if dim.kind == "count":
        if not dim.normalize_ceiling:
            return 0.0
        return min(len(matched), dim.normalize_ceiling) / dim.normalize_ceiling
    if dim.kind == "absence":
        if not dim.normalize_cap:
            return 1.0
        return max(0.0, 1.0 - min(len(matched), dim.normalize_cap) / dim.normalize_cap)
    # Unreachable — profile loader validates kind.
    raise AssertionError(f"unknown dimension kind: {dim.kind}")


def _matches_filter(e: Evidence, dim: Dimension) -> bool:
    """Apply the profile's require_field_not filter, if any."""
    if dim.require_field is None:
        return True
    value = e.extracted_value.get(dim.require_field)
    return value != dim.require_field_not_value


def _measure_contradiction(
    dim: Dimension, matched: list[Evidence]
) -> float:
    """
    MVP: no multi-extractor consensus yet, so contradiction is 0 unless
    the profile explicitly declares a consensus field AND multiple
    matched items disagree on it.

    This is a hook: Step 15+ (multi-provider judgment) will call in
    here with real data to compare.
    """
    if len(matched) < 2 or dim.require_field is None:
        return 0.0
    values = {e.extracted_value.get(dim.require_field) for e in matched}
    return 1.0 if len(values) > 1 else 0.0


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def _compute_confidence(dim_results: list[DimensionResult]) -> float:
    """
    Confidence = coverage * (1 - avg_contradiction), weighted by the
    profile's dimension weights.

    coverage is dimension-weighted — a missing high-weight dimension
    hurts more than a missing low-weight one.
    """
    if not dim_results:
        return 0.0
    weighted_coverage = sum(
        r.coverage * r.weight for r in dim_results
    )
    # Contradiction penalty averaged over matched dimensions only.
    matched = [r for r in dim_results if r.coverage > 0]
    if matched:
        avg_contradiction = sum(r.contradiction for r in matched) / len(matched)
    else:
        avg_contradiction = 0.0
    conf = weighted_coverage * (1.0 - avg_contradiction)
    return _round3(conf)


# ---------------------------------------------------------------------------
# The determinism-defining hash
# ---------------------------------------------------------------------------

def _compute_hash(profile: Profile, dim_results: list[DimensionResult]) -> str:
    """
    SHA256 over the profile hash + a canonical form of the results.
    Same profile + same evidence ids in the same dimensions → same hash.

    Any change in evidence set, dimension score, or profile invalidates
    the hash. That's what makes 'byte for byte' verifiable.
    """
    payload = {
        "profile_hash": profile.profile_hash,
        "dimensions": [
            {
                "name": r.dimension_name,
                "raw_score": _decimal_str(r.raw_score),
                "weight": _decimal_str(r.weight),
                "weighted_score": _decimal_str(r.weighted_score),
                "coverage": _decimal_str(r.coverage),
                "contradiction": _decimal_str(r.contradiction),
                "evidence_count": r.evidence_count,
                "evidence_ids": list(r.evidence_ids),   # already sorted
            }
            for r in dim_results
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Numeric utilities — round to 3 decimals to match Migration 006's precision
# ---------------------------------------------------------------------------

def _round3(x: float) -> float:
    """Round to 3 decimals with half-up rounding. Matches Postgres NUMERIC
    behaviour and keeps the hash stable across float-precision noise."""
    return float(
        Decimal(str(x)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )


def _decimal_str(x: float | Decimal) -> str:
    """Stable string form for the hash — always exactly 3 decimals."""
    return format(
        Decimal(str(x)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
        "f",
    )
