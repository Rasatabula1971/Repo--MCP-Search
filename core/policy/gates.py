"""
Policy gates — Step 10.

Four gates for the MVP slice. Each is a pure function of the evidence
set + a small context object. No DB access; the orchestrator layer
handles persistence.

Each gate declares:
  - name:        stable identifier written to gate_result
  - is_blocking: whether a `fail` result blocks publication by default
  - evaluate(...) -> GateEvaluation

Adding a gate later means writing a new class here and registering it
in default_gates(). Nothing else in the pipeline needs to know.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class GateStatus(str, Enum):
    PASS          = "pass"
    WARN          = "warn"
    FAIL          = "fail"
    NOT_EVALUATED = "not_evaluated"


# ---------------------------------------------------------------------------
# Input + output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateEvidence:
    """One evidence item, as the policy layer sees it. Same shape as
    scoring's Evidence — the policy layer never reads locators either."""
    id: str
    evidence_type: str
    extracted_value: dict


@dataclass(frozen=True)
class GateContext:
    """
    Non-evidence facts about the capability_version being evaluated.
    Kept small — anything a gate needs beyond evidence must be added
    here explicitly rather than fetched ad hoc, so the pure-function
    property survives.
    """
    has_source_binding: bool
    content_hash: str | None


@dataclass(frozen=True)
class GateEvaluation:
    status: GateStatus
    reason: str | None = None
    detail: dict = field(default_factory=dict)


@runtime_checkable
class Gate(Protocol):
    name: str
    is_blocking: bool

    def evaluate(
        self,
        evidence: Iterable[GateEvidence],
        context: GateContext,
    ) -> GateEvaluation: ...


# ---------------------------------------------------------------------------
# License policy — the allowlist as data, kept next to the gate that uses it
# ---------------------------------------------------------------------------

# Permissive + weak-copyleft. Deliberately narrow. GPL/LGPL flavors are
# not included because they carry redistribution obligations that
# affect the depending capability's own license. Adding a license here
# is a policy call.
ACCEPTABLE_LICENSES: frozenset[str] = frozenset({
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "MPL-2.0",
    "Unlicense",
})


class LicenseCompatibilityGate:
    """
    Fails on: missing license, unknown SPDX id, or SPDX id not on the
    ACCEPTABLE_LICENSES allowlist.

    Blocking. A capability without a compatible license cannot be
    published — copyright fallout is not an override-with-a-note kind
    of risk.
    """
    name = "license_compatibility"
    is_blocking = True

    def evaluate(self, evidence, context):
        licenses = [
            e for e in evidence if e.evidence_type == "license"
        ]
        if not licenses:
            return GateEvaluation(
                status=GateStatus.FAIL,
                reason="no license evidence found",
            )

        spdx_ids = {
            e.extracted_value.get("spdx_id") for e in licenses
        }
        if spdx_ids == {"unknown"}:
            return GateEvaluation(
                status=GateStatus.FAIL,
                reason="license file present but SPDX id unknown",
                detail={"spdx_ids": sorted(x for x in spdx_ids if x)},
            )

        acceptable = spdx_ids & ACCEPTABLE_LICENSES
        if acceptable:
            return GateEvaluation(
                status=GateStatus.PASS,
                reason=f"acceptable license(s): {sorted(acceptable)}",
                detail={"spdx_ids": sorted(x for x in spdx_ids if x)},
            )

        return GateEvaluation(
            status=GateStatus.FAIL,
            reason="no acceptable license detected",
            detail={
                "spdx_ids": sorted(x for x in spdx_ids if x),
                "allowlist": sorted(ACCEPTABLE_LICENSES),
            },
        )


class CriticalVulnerabilityGate:
    """
    MVP proxy: any secret_indicator evidence is treated as a critical
    finding. Real CVE scanning arrives later; the shape (evidence-driven,
    blocking) is what matters at Step 10.
    """
    name = "critical_vulnerability"
    is_blocking = True

    def evaluate(self, evidence, context):
        secrets = [
            e for e in evidence if e.evidence_type == "secret_indicator"
        ]
        if not secrets:
            return GateEvaluation(
                status=GateStatus.PASS,
                reason="no secret indicators found",
            )
        return GateEvaluation(
            status=GateStatus.FAIL,
            reason=f"{len(secrets)} secret indicator(s) in source",
            detail={
                "count": len(secrets),
                "patterns": sorted({
                    e.extracted_value.get("pattern_name")
                    for e in secrets
                    if e.extracted_value.get("pattern_name")
                }),
            },
        )


class MissingProvenanceGate:
    """
    Sanity gate: a capability_version must have a source binding and
    that binding's revision must have a content_hash. In the normal
    flow this always passes; the gate exists as a defensive check
    against manual data manipulation.
    """
    name = "missing_provenance"
    is_blocking = True

    def evaluate(self, evidence, context):
        if not context.has_source_binding:
            return GateEvaluation(
                status=GateStatus.FAIL,
                reason="capability_version has no source_binding",
            )
        if not context.content_hash:
            return GateEvaluation(
                status=GateStatus.FAIL,
                reason="bound revision has no content_hash",
            )
        return GateEvaluation(
            status=GateStatus.PASS,
            reason="source binding + content hash present",
        )


class UnsafeDeclaredPermissionsGate:
    """
    Advisory. If setup.py is present, install-time code execution is
    possible — we can't know without running it what it does. WARN
    rather than FAIL so it doesn't block publication on its own; the
    reviewer sees the warning and decides.
    """
    name = "unsafe_declared_permissions"
    is_blocking = False    # advisory, not blocking

    def evaluate(self, evidence, context):
        setup_py = [
            e for e in evidence if e.evidence_type == "setup_py_detected"
        ]
        if setup_py:
            return GateEvaluation(
                status=GateStatus.WARN,
                reason=(
                    "setup.py present; install-time execution is possible "
                    "and could not be inspected statically"
                ),
                detail={"count": len(setup_py)},
            )
        return GateEvaluation(
            status=GateStatus.PASS,
            reason="no opaque install-time code declared",
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def default_gates() -> list[Gate]:
    """The stock lineup. Callers can pass a different list for tests."""
    return [
        LicenseCompatibilityGate(),
        CriticalVulnerabilityGate(),
        MissingProvenanceGate(),
        UnsafeDeclaredPermissionsGate(),
    ]
