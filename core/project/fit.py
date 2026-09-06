"""
Fit evaluation — pure function of (requirement, candidate_inputs).

Given a set of requirement constraints and a candidate's evidence,
produce a FitResult with fit_score, gaps, and evidence links.

Determinism: same inputs → same FitResult (including computed_hash).
No DB, no external calls.

Fit score formula:
  For each constraint, produce a 'contribution' in [0, 1] and mark
  it satisfied or unsatisfied. fit_score = (sum of contributions) /
  (count of constraints). If no constraints, fit_score = 1.0 with a
  documented note.

Constraint kinds supported (extensible by adding branches here):
  - required_interface   — capability must expose an interface with
                            a matching name (case-insensitive) and,
                            optionally, a substring of the signature
  - license_allowlist    — capability must have a license evidence
                            whose spdx_id is in the allowlist
  - forbidden_dependency — capability must NOT declare a dep with a
                            matching (ecosystem, name)
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

from core.project.types import (
    CandidateInputs,
    Constraint,
    FitEvidenceLink,
    FitGap,
    FitResult,
    Requirement,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def evaluate_fit(
    requirement: Requirement, candidate: CandidateInputs
) -> FitResult:
    """
    Pure fit computation. Same inputs → identical FitResult.
    """
    gaps: list[FitGap] = []
    supports: list[FitEvidenceLink] = []
    gap_evidence: list[FitEvidenceLink] = []

    total = 0.0
    count = 0

    for constraint in requirement.constraints:
        contribution, satisfied, gap, matched_ev, gap_ev = _apply_constraint(
            constraint, candidate,
        )
        total += contribution
        count += 1
        if gap is not None:
            gaps.append(gap)
        supports.extend(matched_ev)
        gap_evidence.extend(gap_ev)

    fit_score = _round3(total / count) if count > 0 else 1.0

    # Dedupe evidence links while preserving order — same evidence in
    # the same role should link once, not per-mention.
    seen: set[tuple[str, str]] = set()
    unique_links: list[FitEvidenceLink] = []
    for link in supports + gap_evidence:
        key = (link.evidence_item_id, link.role)
        if key in seen:
            continue
        seen.add(key)
        unique_links.append(link)

    blocking = sum(1 for g in gaps if g.is_blocking)
    computed_hash = _compute_hash(
        requirement=requirement,
        candidate=candidate,
        fit_score=fit_score,
        gaps=gaps,
        links=unique_links,
    )

    return FitResult(
        fit_score=fit_score,
        blocking_gap_count=blocking,
        gaps=tuple(gaps),
        evidence_links=tuple(unique_links),
        computed_hash=computed_hash,
    )


# ---------------------------------------------------------------------------
# Per-constraint matchers
# ---------------------------------------------------------------------------

def _apply_constraint(
    c: Constraint, cand: CandidateInputs,
) -> tuple[float, bool, FitGap | None, list[FitEvidenceLink], list[FitEvidenceLink]]:
    """
    Returns (contribution, satisfied, gap, matched_evidence_links, gap_links).
    contribution is added to the numerator of fit_score; count is always +1
    at the caller.
    """
    if c.kind == "required_interface":
        return _match_required_interface(c, cand)
    if c.kind == "license_allowlist":
        return _match_license_allowlist(c, cand)
    if c.kind == "forbidden_dependency":
        return _match_forbidden_dependency(c, cand)
    # Unknown constraint kind = advisory gap, contributes 0.
    gap = FitGap(
        kind="unknown_constraint_kind",
        is_blocking=False,
        detail={"kind": c.kind, "note": "no matcher registered"},
    )
    return 0.0, False, gap, [], []


def _match_required_interface(c, cand):
    want_name = str(c.detail.get("name", "")).strip().lower()
    want_sig_frag = str(c.detail.get("signature_contains", "")).strip()

    if not want_name:
        gap = FitGap(kind="malformed_constraint", is_blocking=False,
                     detail={"kind": "required_interface", "reason": "no name"})
        return 0.0, False, gap, [], []

    matches = [
        iface for iface in cand.interfaces
        if iface.name.lower() == want_name
        and (not want_sig_frag or want_sig_frag in (iface.signature or ""))
    ]
    if matches:
        return (
            1.0, True, None,
            [FitEvidenceLink(evidence_item_id=m.evidence_item_id,
                              role="supports_match") for m in matches],
            [],
        )

    # No match — gap. Non-blocking by default; missing an interface
    # is a fit issue but not a legal/security issue.
    gap = FitGap(
        kind="missing_interface",
        is_blocking=False,
        detail={"want_name": want_name, "want_signature_contains": want_sig_frag},
    )
    return 0.0, False, gap, [], []


def _match_license_allowlist(c, cand):
    allow = {s.upper() for s in c.detail.get("spdx_ids", [])}
    if not allow:
        gap = FitGap(kind="malformed_constraint", is_blocking=False,
                     detail={"kind": "license_allowlist", "reason": "empty allowlist"})
        return 0.0, False, gap, [], []

    if not cand.licenses:
        # No license evidence at all — blocking.
        gap = FitGap(
            kind="license_mismatch",
            is_blocking=True,
            detail={"reason": "capability has no identified license",
                    "allowlist": sorted(allow)},
        )
        return 0.0, False, gap, [], []

    matching = [
        lic for lic in cand.licenses
        if lic.spdx_id and lic.spdx_id.upper() in allow
    ]
    if matching:
        return (
            1.0, True, None,
            [FitEvidenceLink(evidence_item_id=lic.evidence_item_id,
                              role="supports_match") for lic in matching],
            [],
        )

    # Has licenses, none acceptable — blocking.
    observed = sorted({lic.spdx_id for lic in cand.licenses if lic.spdx_id})
    gap = FitGap(
        kind="license_mismatch",
        is_blocking=True,
        detail={"observed": observed, "allowlist": sorted(allow)},
    )
    gap_links = [
        FitEvidenceLink(evidence_item_id=lic.evidence_item_id,
                         role="documents_gap")
        for lic in cand.licenses
    ]
    return 0.0, False, gap, [], gap_links


def _match_forbidden_dependency(c, cand):
    ecosystem = str(c.detail.get("ecosystem", "")).lower()
    name = str(c.detail.get("name", "")).lower()
    if not ecosystem or not name:
        gap = FitGap(
            kind="malformed_constraint", is_blocking=False,
            detail={"kind": "forbidden_dependency",
                    "reason": "need ecosystem and name"},
        )
        return 0.0, False, gap, [], []

    hits = [
        d for d in cand.dependencies
        if d.ecosystem.lower() == ecosystem and d.name.lower() == name
    ]
    if not hits:
        # Absence is presence-of-safety here — the constraint is
        # satisfied by NOT finding the forbidden dep. No evidence
        # to link (proving a negative), so we just report satisfied.
        return 1.0, True, None, [], []

    gap = FitGap(
        kind="forbidden_dependency_present",
        is_blocking=True,
        detail={"ecosystem": ecosystem, "name": name,
                "occurrences": len(hits)},
    )
    gap_links = [
        FitEvidenceLink(evidence_item_id=h.evidence_item_id,
                         role="documents_gap")
        for h in hits
    ]
    return 0.0, False, gap, [], gap_links


# ---------------------------------------------------------------------------
# Determinism plumbing
# ---------------------------------------------------------------------------

def _round3(x: float) -> float:
    return float(
        Decimal(str(x)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )


def _compute_hash(
    *, requirement: Requirement, candidate: CandidateInputs,
    fit_score: float, gaps: list[FitGap], links: list[FitEvidenceLink],
) -> str:
    payload = {
        "requirement_id": requirement.id,
        "capability_version_id": candidate.capability_version_id,
        "constraint_shape": [
            {"kind": c.kind, "detail": _canonical(c.detail)}
            for c in requirement.constraints
        ],
        "fit_score": format(
            Decimal(str(fit_score)).quantize(
                Decimal("0.001"), rounding=ROUND_HALF_UP,
            ),
            "f",
        ),
        "gaps": sorted(
            [{"kind": g.kind, "is_blocking": g.is_blocking,
              "detail": _canonical(g.detail)} for g in gaps],
            key=lambda g: (g["kind"], json.dumps(g["detail"], sort_keys=True)),
        ),
        "links": sorted(
            [{"evidence_item_id": l.evidence_item_id, "role": l.role}
             for l in links],
            key=lambda l: (l["evidence_item_id"], l["role"]),
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical(obj):
    """Recursively canonicalise dicts/lists for stable hashing."""
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonical(x) for x in obj]
    return obj
