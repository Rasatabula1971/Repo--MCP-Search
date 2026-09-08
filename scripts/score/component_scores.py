"""
Per-kind component scorer (Phase 3d).

Computes deterministic health scores from capability metadata. One
profile per component_kind. No LLM. Scores are written to
component_score with (profile_name, profile_version) so a re-run at
the same version is idempotent, and bumping a profile version
re-scores everything under that profile.

Dimensions differ by kind:
  repo    -> freshness, popularity, license_clarity, description_quality, activity
  library -> license_clarity, description_quality, ecosystem_bonus
  skill   -> completeness, description_quality, license_clarity
  mcp_tool-> transport_clarity, description_quality
  agent   -> description_quality
  workflow_template -> completeness

Every dimension is a 0.0-1.0 float. Total = weighted sum of dimensions
(weights per kind sum to 1.0). Confidence reflects how many
dimensions had real data vs. defaulted to 0.5.

Usage:
    python -m scripts.score.component_scores
    python -m scripts.score.component_scores --kind repo
    python -m scripts.score.component_scores --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from db.connection import connect
from scripts.ingest import _base

SOURCE_NAME     = "score_components"
PROFILE_VERSION = 1


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------

@dataclass
class DimensionResult:
    raw: float           # 0.0 .. 1.0
    weight: float        # weights per profile sum to 1.0
    has_evidence: bool   # False when we defaulted to 0.5

    @property
    def weighted(self) -> float:
        return round(self.raw * self.weight, 4)


@dataclass
class ScoreOutcome:
    profile_name: str
    total: float
    confidence: float
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _round3(x: float) -> float:
    return round(x, 3)


def _combine(profile_name: str,
             dims: dict[str, DimensionResult]) -> ScoreOutcome:
    total = sum(d.weighted for d in dims.values())
    n_with_evidence = sum(1 for d in dims.values() if d.has_evidence)
    confidence = _round3(n_with_evidence / len(dims)) if dims else 0.0
    return ScoreOutcome(
        profile_name=profile_name,
        total=_round3(_clip(total)),
        confidence=confidence,
        dimensions=dims,
    )


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _pushed_at_days_ago(meta: dict) -> Optional[float]:
    """Days since pushed_at. None if not available or unparseable."""
    ts = meta.get("pushed_at") or meta.get("updated_at")
    if not ts:
        return None
    try:
        # GitHub returns ISO 8601 with Z suffix.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def _freshness_score(days: Optional[float]) -> DimensionResult:
    """1.0 for 0 days, 0.0 for 730+ days, linear between. 0.5 if unknown."""
    if days is None:
        return DimensionResult(raw=0.5, weight=0.0, has_evidence=False)
    return DimensionResult(raw=_clip(1.0 - (days / 730.0)), weight=0.0,
                            has_evidence=True)


def _popularity_score(stars: Optional[int]) -> DimensionResult:
    """Log-normalized: 0 stars -> 0.0, 10 -> 0.25, 100 -> 0.5, 1k -> 0.75,
       10k+ -> 1.0."""
    if stars is None:
        return DimensionResult(raw=0.5, weight=0.0, has_evidence=False)
    if stars <= 0:
        raw = 0.0
    else:
        raw = _clip(math.log10(stars + 1) / 4.0)   # log10(10001) ~ 4.0
    return DimensionResult(raw=raw, weight=0.0, has_evidence=True)


def _activity_score(open_issues: Optional[int],
                     forks: Optional[int]) -> DimensionResult:
    """Presence of issues + forks == community activity. Coarse."""
    if open_issues is None and forks is None:
        return DimensionResult(raw=0.5, weight=0.0, has_evidence=False)
    signal = 0.0
    if open_issues is not None and open_issues > 0:
        signal += 0.5
    if forks is not None and forks > 0:
        signal += 0.5
    return DimensionResult(raw=_clip(signal), weight=0.0, has_evidence=True)


def _license_clarity_score(license_spdx: Optional[str]) -> DimensionResult:
    """Known SPDX id = 1.0, missing = 0.3."""
    if license_spdx:
        return DimensionResult(raw=1.0, weight=0.0, has_evidence=True)
    return DimensionResult(raw=0.3, weight=0.0, has_evidence=True)


def _description_quality_score(meta: dict) -> DimensionResult:
    """gemini_description present -> 1.0, only raw description -> 0.6, nothing -> 0.0."""
    if meta.get("gemini_description"):
        return DimensionResult(raw=1.0, weight=0.0, has_evidence=True)
    if meta.get("description") or meta.get("seed_note"):
        return DimensionResult(raw=0.6, weight=0.0, has_evidence=True)
    return DimensionResult(raw=0.0, weight=0.0, has_evidence=True)


def _ecosystem_bonus_score(ecosystem: str) -> DimensionResult:
    """Known ecosystems (pypi, npm) score full; source-only scores half."""
    if ecosystem in ("pypi", "npm", "cargo", "gem"):
        raw = 1.0
    elif ecosystem == "source":
        raw = 0.5
    else:
        raw = 0.3
    return DimensionResult(raw=raw, weight=0.0, has_evidence=True)


def _completeness_score(meta: dict) -> DimensionResult:
    """Number of populated metadata fields as a rough completeness signal."""
    fields = ["gemini_description", "description", "content_hash", "topics",
              "language", "html_url", "seed_note"]
    populated = sum(1 for f in fields if meta.get(f))
    return DimensionResult(raw=_clip(populated / 4.0), weight=0.0,
                            has_evidence=populated > 0)


def _transport_clarity_score(meta: dict) -> DimensionResult:
    """MCP servers with a known transport score full."""
    t = (meta.get("transport") or "").lower()
    if t in ("stdio", "sse", "http"):
        return DimensionResult(raw=1.0, weight=0.0, has_evidence=True)
    return DimensionResult(raw=0.3, weight=0.0, has_evidence=True)


# ---------------------------------------------------------------------------
# Per-kind scoring functions
# ---------------------------------------------------------------------------

def _score_repo(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {
        "freshness":            _freshness_score(_pushed_at_days_ago(meta)),
        "popularity":           _popularity_score(meta.get("stars")),
        "activity":             _activity_score(meta.get("open_issues"),
                                                  meta.get("forks")),
        "license_clarity":      _license_clarity_score(cap.get("license_spdx")),
        "description_quality":  _description_quality_score(meta),
    }
    # Weights sum to 1.0. Freshness + popularity carry the most because
    # they're the strongest signal that a repo is worth adopting.
    dims["freshness"].weight            = 0.30
    dims["popularity"].weight           = 0.30
    dims["activity"].weight             = 0.10
    dims["license_clarity"].weight      = 0.15
    dims["description_quality"].weight  = 0.15
    return _combine("repo_health", dims)


def _score_library(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {
        "license_clarity":      _license_clarity_score(cap.get("license_spdx")),
        "description_quality":  _description_quality_score(meta),
        "ecosystem_bonus":      _ecosystem_bonus_score(cap.get("ecosystem", "")),
    }
    dims["license_clarity"].weight      = 0.35
    dims["description_quality"].weight  = 0.40
    dims["ecosystem_bonus"].weight      = 0.25
    return _combine("library_health", dims)


def _score_skill(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {
        "completeness":         _completeness_score(meta),
        "description_quality":  _description_quality_score(meta),
        "license_clarity":      _license_clarity_score(cap.get("license_spdx")),
    }
    dims["completeness"].weight         = 0.50
    dims["description_quality"].weight  = 0.40
    dims["license_clarity"].weight      = 0.10
    return _combine("skill_health", dims)


def _score_mcp_tool(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {
        "transport_clarity":    _transport_clarity_score(meta),
        "description_quality":  _description_quality_score(meta),
    }
    dims["transport_clarity"].weight    = 0.60
    dims["description_quality"].weight  = 0.40
    return _combine("mcp_tool_health", dims)


def _score_agent(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {"description_quality": _description_quality_score(meta)}
    dims["description_quality"].weight = 1.0
    return _combine("agent_health", dims)


def _score_workflow_template(cap: dict) -> ScoreOutcome:
    meta = cap.get("metadata") or {}
    dims = {"completeness": _completeness_score(meta)}
    dims["completeness"].weight = 1.0
    return _combine("workflow_template_health", dims)


SCORERS = {
    "repo":               _score_repo,
    "library":            _score_library,
    "skill":              _score_skill,
    "mcp_tool":           _score_mcp_tool,
    "agent":              _score_agent,
    "workflow_template":  _score_workflow_template,
}


def score_component(cap: dict) -> Optional[ScoreOutcome]:
    scorer = SCORERS.get(cap.get("component_kind"))
    if scorer is None:
        return None
    return scorer(cap)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _serialize_dimensions(dims: dict[str, DimensionResult]) -> dict:
    return {
        name: {"raw": round(d.raw, 4),
               "weight": round(d.weight, 4),
               "weighted": d.weighted,
               "has_evidence": d.has_evidence}
        for name, d in dims.items()
    }


def upsert_score(conn, capability_id: str, outcome: ScoreOutcome) -> str:
    """
    Returns 'new' | 'unchanged'. UNIQUE(capability_id, profile_name,
    profile_version) means re-running at the same version is a no-op.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_score, confidence FROM component_score "
            "WHERE capability_id = %s "
            "  AND profile_name = %s AND profile_version = %s",
            (capability_id, outcome.profile_name, PROFILE_VERSION),
        )
        existing = cur.fetchone()
        if existing:
            if (float(existing[0]), float(existing[1])) == (outcome.total, outcome.confidence):
                return "unchanged"
            cur.execute(
                "UPDATE component_score SET "
                "  total_score = %s, confidence = %s, dimensions = %s::jsonb, "
                "  computed_at = now() "
                "WHERE capability_id = %s "
                "  AND profile_name = %s AND profile_version = %s",
                (outcome.total, outcome.confidence,
                 json.dumps(_serialize_dimensions(outcome.dimensions)),
                 capability_id, outcome.profile_name, PROFILE_VERSION),
            )
            return "updated"
        cur.execute(
            "INSERT INTO component_score "
            "(capability_id, profile_name, profile_version, "
            " total_score, confidence, dimensions) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            (capability_id, outcome.profile_name, PROFILE_VERSION,
             outcome.total, outcome.confidence,
             json.dumps(_serialize_dimensions(outcome.dimensions))),
        )
        return "new"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _select_rows(conn, kind: Optional[str]) -> list[dict]:
    sql = (
        "SELECT id::text, normalized_key, display_name, ecosystem, kind, "
        "       component_kind, license_spdx, metadata "
        "FROM capability "
        "WHERE %s::text IS NULL OR component_kind = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (kind, kind))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_scoring(kind: Optional[str] = None,
                dry_run: bool = False) -> dict[str, Any]:
    conn = connect()
    metadata = {"kind": kind, "profile_version": PROFILE_VERSION,
                "dry_run": dry_run}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            rows = _select_rows(conn, kind)
            print(f"({len(rows)} row(s) to score)")
            for cap in rows:
                outcome = score_component(cap)
                if outcome is None:
                    counts.errors += 1
                    print(f"  no_scorer   {cap['normalized_key']}  "
                          f"kind={cap['component_kind']}", file=sys.stderr)
                    continue
                if dry_run:
                    print(f"  dry-run    {cap['normalized_key']:60s}"
                          f"  {outcome.profile_name}={outcome.total:.3f}"
                          f" (conf {outcome.confidence:.2f})")
                    counts.unchanged += 1
                    continue
                try:
                    written = upsert_score(conn, cap["id"], outcome)
                    conn.commit()
                    if written == "new":       counts.new += 1
                    elif written == "updated":   counts.updated += 1
                    else:                          counts.unchanged += 1
                    print(f"  {written:10s}  {cap['normalized_key']:60s}"
                          f"  {outcome.profile_name}={outcome.total:.3f}"
                          f" (conf {outcome.confidence:.2f})")
                except Exception as e:
                    counts.errors += 1
                    conn.rollback()
                    print(f"  error       {cap['normalized_key']}: {e}",
                          file=sys.stderr)
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", default=None, help="Only score this component_kind.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run_scoring(kind=args.kind, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
