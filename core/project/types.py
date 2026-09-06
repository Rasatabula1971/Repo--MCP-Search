"""
Project-domain types — Step 12.

Pure dataclasses. No DB, no IO. The orchestrator in workers/recommend
loads from DB, calls the pure functions in core.project.fit +
core.project.rules, then persists results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Requirement + constraints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Constraint:
    """
    A single constraint. `kind` names the matcher; `detail` is a small
    dict interpreted by that matcher. Adding a kind means adding a
    branch in fit._apply_constraint.
    """
    kind: str
    detail: dict


@dataclass(frozen=True)
class Requirement:
    id: str                              # UUID as string
    slug: str
    description: str
    constraints: tuple[Constraint, ...]


# ---------------------------------------------------------------------------
# Candidate — everything a fit computation needs about a capability version
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InterfaceEvidence:
    """
    A public interface exposed by the capability (from
    capability_interface). evidence_item_id lets fit_evidence_link
    trace back to the original observation.
    """
    evidence_item_id: str
    kind: str                    # 'function' | 'async_function' | 'class'
    name: str
    signature: str
    language: str | None = None


@dataclass(frozen=True)
class DependencyEvidence:
    evidence_item_id: str
    ecosystem: str
    name: str
    kind: str                    # 'runtime' | 'dev' | 'peer' | 'optional:...'


@dataclass(frozen=True)
class LicenseEvidence:
    evidence_item_id: str
    spdx_id: str


@dataclass(frozen=True)
class CandidateInputs:
    """
    Everything about a capability_version that fit + rules can see. If
    a matcher needs a new signal, it's added here — not fetched ad hoc
    from the DB — so the pure-function property survives.
    """
    capability_version_id: str
    intrinsic_score: float               # from scorecard.total_score
    interfaces: tuple[InterfaceEvidence, ...]
    dependencies: tuple[DependencyEvidence, ...]
    licenses: tuple[LicenseEvidence, ...]
    pinned_revision_key: str             # the exact SHA to name
    pinned_source_asset_key: str         # e.g. 'psf/requests'


# ---------------------------------------------------------------------------
# Fit output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FitGap:
    kind: str                    # 'missing_interface' | 'license_mismatch' | ...
    is_blocking: bool
    detail: dict


@dataclass(frozen=True)
class FitEvidenceLink:
    evidence_item_id: str
    role: str                    # 'supports_match' | 'documents_gap'


@dataclass(frozen=True)
class FitResult:
    """
    Output of core.project.fit.evaluate_fit. Pure — no DB writes here.
    """
    fit_score: float             # 0..1, rounded to 3 decimals
    blocking_gap_count: int
    gaps: tuple[FitGap, ...]
    evidence_links: tuple[FitEvidenceLink, ...]
    computed_hash: str


# ---------------------------------------------------------------------------
# Verdict + rule decision
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    ADOPT     = "ADOPT"          # use as-is
    ADAPT     = "ADAPT"          # use with minor changes
    WRAP      = "WRAP"           # build a thin wrapper
    REFERENCE = "REFERENCE"      # reuse ideas/pieces, don't depend
    REJECT    = "REJECT"         # none of the candidates work
    BUILD     = "BUILD"          # build new (needs Step 13's gate)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule_name: str
    reason: str


# ---------------------------------------------------------------------------
# Combined outcome — what the orchestrator ranks + persists
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate paired with its fit_result and intrinsic_score,
    ready for ranking + rule evaluation."""
    inputs: CandidateInputs
    fit_result: FitResult
