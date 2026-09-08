"""
Pure project-constraint evaluator (Phase 4).

Given a project's constraint set and a capability's material (metadata,
component_kind, runtime, cost_tier, license_spdx), produces a verdict:
does it pass, and if not, why.

No DB, no IO, no LLM. This module is imported by browse_components,
by the constraint-fit MCP tool, and by any future scoring pipeline
that wants to score-adjust for project constraints.

Constraint kinds and semantics:

  cpu_only / no_gpu       Hard-fail if metadata declares a GPU need or
                          topics include 'gpu', 'cuda', 'nvidia'.
  must_be_free_tier       Hard-fail if cost_tier is 'paid' or 'cheap_paid'.
                          Unknown cost_tier passes with reason='unknown_cost'.
  must_be_local           Hard-fail if runtime is a remote transport
                          (http_endpoint, mcp_http, mcp_sse pointing off-box).
  always_off_at_night     Hard-fail if the component's kind/nature
                          requires an always-on server (celery-like broker
                          workers, http_endpoint runtimes).
  budget_monthly_ceiling  {amount_usd: N}. Hard-fail if metadata declares
                          a fixed monthly cost above N. Unknown = pass.
  license_allowlist       {spdx_ids: [...]}. Hard-fail if license_spdx is
                          set AND NOT in the list. Unknown license passes
                          with reason='unknown_license'.
  license_denylist        {spdx_ids: [...]}. Hard-fail if license_spdx is
                          set AND in the list.
  runtime_allowlist       {runtimes: [...]}. Hard-fail if runtime is set
                          AND NOT in the list.
  component_kind_allowlist {kinds: [...]}. Hard-fail if component_kind
                          NOT in the list.

Semantics on unknowns: we DON'T hard-fail for missing metadata (that
would over-reject during the messy ingestion phase). Instead the
verdict carries reason='unknown_*' so the caller can decide whether
to suppress-with-warning vs. show-as-uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


REMOTE_RUNTIMES  = {"http_endpoint", "mcp_http", "mcp_sse"}
GPU_HINTS        = {"gpu", "cuda", "nvidia", "rocm", "tensorrt"}


@dataclass
class ConstraintVerdict:
    kind: str
    passed: bool
    reason: str          # short slug: 'ok' / 'gpu_required' / 'paid' / 'unknown_license' / ...
    detail: str = ""     # human-readable extra detail

    @property
    def hard_fails(self) -> bool:
        return not self.passed


@dataclass
class FitResult:
    hard_fail: bool
    verdicts: list[ConstraintVerdict] = field(default_factory=list)

    @property
    def fail_reasons(self) -> list[str]:
        return [f"{v.kind}:{v.reason}" for v in self.verdicts if v.hard_fails]


# ---------------------------------------------------------------------------
# Component "material" — the shape of data the evaluator needs from a row
# ---------------------------------------------------------------------------

@dataclass
class ComponentMaterial:
    component_kind: str
    runtime: str | None
    cost_tier: str | None
    license_spdx: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ComponentMaterial":
        return cls(
            component_kind=row.get("component_kind") or "",
            runtime=row.get("runtime"),
            cost_tier=row.get("cost_tier"),
            license_spdx=row.get("license_spdx"),
            metadata=row.get("metadata") or {},
        )


# ---------------------------------------------------------------------------
# Individual constraint checkers
# ---------------------------------------------------------------------------

def _check_no_gpu(_detail, m: ComponentMaterial) -> ConstraintVerdict:
    if bool(m.metadata.get("requires_gpu")):
        return ConstraintVerdict("no_gpu", False, "gpu_required",
                                   "metadata.requires_gpu is truthy")
    topics = {t.lower() for t in (m.metadata.get("topics") or []) if isinstance(t, str)}
    hit = topics & GPU_HINTS
    if hit:
        return ConstraintVerdict("no_gpu", False, "gpu_topic",
                                   f"topic tags include {sorted(hit)}")
    return ConstraintVerdict("no_gpu", True, "ok")


def _check_must_be_free_tier(_detail, m: ComponentMaterial) -> ConstraintVerdict:
    tier = (m.cost_tier or "").lower()
    if tier in ("paid", "cheap_paid"):
        return ConstraintVerdict("must_be_free_tier", False, "paid",
                                   f"cost_tier={tier}")
    if tier in ("", "unknown"):
        return ConstraintVerdict("must_be_free_tier", True, "unknown_cost",
                                   "cost_tier missing; not blocking")
    return ConstraintVerdict("must_be_free_tier", True, "ok")


def _check_must_be_local(_detail, m: ComponentMaterial) -> ConstraintVerdict:
    rt = (m.runtime or "").lower()
    if rt in REMOTE_RUNTIMES:
        return ConstraintVerdict("must_be_local", False, "remote_runtime",
                                   f"runtime={rt}")
    return ConstraintVerdict("must_be_local", True, "ok")


def _check_always_off_at_night(_detail, m: ComponentMaterial) -> ConstraintVerdict:
    """
    Heuristic: components whose purpose is running a continuous
    background broker/queue are the disqualifiers. We look at both
    normalized_key hints and topic hints.
    """
    rt = (m.runtime or "").lower()
    if rt in REMOTE_RUNTIMES:
        return ConstraintVerdict("always_off_at_night", False, "remote_runtime",
                                   f"runtime={rt} implies always-on server")
    bad_topics = {"celery", "airflow", "kafka", "rabbitmq", "cron", "scheduler"}
    topics = {t.lower() for t in (m.metadata.get("topics") or []) if isinstance(t, str)}
    hit = topics & bad_topics
    if hit:
        return ConstraintVerdict("always_off_at_night", False, "requires_broker",
                                   f"topics include {sorted(hit)}")
    return ConstraintVerdict("always_off_at_night", True, "ok")


def _check_budget_monthly_ceiling(detail, m: ComponentMaterial) -> ConstraintVerdict:
    ceiling = detail.get("amount_usd")
    if ceiling is None:
        return ConstraintVerdict("budget_monthly_ceiling", True, "no_ceiling")
    declared = m.metadata.get("monthly_cost_usd")
    if declared is None:
        return ConstraintVerdict("budget_monthly_ceiling", True, "unknown_cost",
                                   "metadata.monthly_cost_usd missing")
    if float(declared) > float(ceiling):
        return ConstraintVerdict("budget_monthly_ceiling", False, "over_budget",
                                   f"${declared}/mo > ${ceiling}")
    return ConstraintVerdict("budget_monthly_ceiling", True, "ok")


def _check_license_allowlist(detail, m: ComponentMaterial) -> ConstraintVerdict:
    allow = {s.upper() for s in (detail.get("spdx_ids") or [])}
    if not m.license_spdx:
        return ConstraintVerdict("license_allowlist", True, "unknown_license",
                                   "license_spdx missing")
    if m.license_spdx.upper() not in allow:
        return ConstraintVerdict("license_allowlist", False, "license_not_allowed",
                                   f"{m.license_spdx} not in allowlist")
    return ConstraintVerdict("license_allowlist", True, "ok")


def _check_license_denylist(detail, m: ComponentMaterial) -> ConstraintVerdict:
    deny = {s.upper() for s in (detail.get("spdx_ids") or [])}
    if not m.license_spdx:
        return ConstraintVerdict("license_denylist", True, "unknown_license")
    if m.license_spdx.upper() in deny:
        return ConstraintVerdict("license_denylist", False, "license_denied",
                                   f"{m.license_spdx} is on denylist")
    return ConstraintVerdict("license_denylist", True, "ok")


def _check_runtime_allowlist(detail, m: ComponentMaterial) -> ConstraintVerdict:
    allow = {s.lower() for s in (detail.get("runtimes") or [])}
    if not m.runtime:
        return ConstraintVerdict("runtime_allowlist", True, "unknown_runtime")
    if m.runtime.lower() not in allow:
        return ConstraintVerdict("runtime_allowlist", False, "runtime_not_allowed",
                                   f"{m.runtime} not in allowlist")
    return ConstraintVerdict("runtime_allowlist", True, "ok")


def _check_component_kind_allowlist(detail, m: ComponentMaterial) -> ConstraintVerdict:
    allow = {s.lower() for s in (detail.get("kinds") or [])}
    if m.component_kind.lower() not in allow:
        return ConstraintVerdict("component_kind_allowlist", False, "kind_not_allowed",
                                   f"{m.component_kind} not in allowlist")
    return ConstraintVerdict("component_kind_allowlist", True, "ok")


# cpu_only is exactly no_gpu.
_CHECKS = {
    "cpu_only":               _check_no_gpu,
    "no_gpu":                 _check_no_gpu,
    "must_be_free_tier":      _check_must_be_free_tier,
    "must_be_local":          _check_must_be_local,
    "always_off_at_night":    _check_always_off_at_night,
    "budget_monthly_ceiling": _check_budget_monthly_ceiling,
    "license_allowlist":      _check_license_allowlist,
    "license_denylist":       _check_license_denylist,
    "runtime_allowlist":      _check_runtime_allowlist,
    "component_kind_allowlist": _check_component_kind_allowlist,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@dataclass
class Constraint:
    kind: str
    detail: dict[str, Any]


def evaluate(
    constraints: list[Constraint],
    material: ComponentMaterial,
) -> FitResult:
    """
    Run every constraint's check against the material. Returns a
    FitResult with per-constraint verdicts and a hard_fail flag that
    is True iff at least one constraint hard-failed.
    """
    verdicts: list[ConstraintVerdict] = []
    for c in constraints:
        checker = _CHECKS.get(c.kind)
        if checker is None:
            verdicts.append(ConstraintVerdict(c.kind, True, "unknown_constraint_kind",
                                                "no checker registered — treated as pass"))
            continue
        verdicts.append(checker(c.detail or {}, material))
    return FitResult(hard_fail=any(v.hard_fails for v in verdicts),
                     verdicts=verdicts)
