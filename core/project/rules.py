"""
Recommendation rules interpreter — Step 12.

Rules are declarative data. This module loads them from YAML, hashes
them stably, and evaluates them against a scored candidate.

The predicate DSL is deliberately tiny:
  fields:    fit_score | intrinsic_score | blocking_gap_count
  operators: == | != | < | <= | > | >=
  values:    numeric literals

Rules are evaluated top to bottom; first match wins. If nothing matches,
the profile's `default` verdict is used.

No arbitrary code eval, no callable predicates — the whole point of
"decisions as data" is that a diff of the YAML is a review-able policy
change, not a code change.
"""
from __future__ import annotations

import hashlib
import json
import operator
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.project.types import Decision, Verdict


_OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<":  operator.lt,
    "<=": operator.le,
    ">":  operator.gt,
    ">=": operator.ge,
}

_ALLOWED_FIELDS = frozenset({"fit_score", "intrinsic_score", "blocking_gap_count"})


class RulesError(ValueError):
    """Raised when a rules profile YAML fails validation."""


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: float

    def check(self, facts: dict) -> bool:
        return _OPERATORS[self.op](facts[self.field], self.value)


@dataclass(frozen=True)
class Rule:
    name: str
    conditions: tuple[Condition, ...]
    verdict: Verdict

    def matches(self, facts: dict) -> bool:
        return all(c.check(facts) for c in self.conditions)


@dataclass(frozen=True)
class RulesProfile:
    name: str
    version: int
    rules: tuple[Rule, ...]
    default: Verdict
    profile_hash: str
    raw: dict


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def load(path: str | Path) -> RulesProfile:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise RulesError("rules profile must be a mapping")
    return _from_dict(raw)


def _from_dict(raw: dict) -> RulesProfile:
    name = raw.get("name")
    version = raw.get("version")
    default_str = raw.get("default", "BUILD")
    rules_raw = raw.get("rules", [])

    if not isinstance(name, str) or not name.strip():
        raise RulesError("rules profile 'name' must be a non-empty string")
    if not isinstance(version, int) or version < 1:
        raise RulesError("rules profile 'version' must be a positive integer")
    try:
        default = Verdict(default_str)
    except ValueError:
        raise RulesError(f"invalid default verdict: {default_str!r}") from None
    if not isinstance(rules_raw, list):
        raise RulesError("rules must be a list")

    rules: list[Rule] = []
    seen_names: set[str] = set()
    for r in rules_raw:
        rule = _rule_from_dict(r)
        if rule.name in seen_names:
            raise RulesError(f"duplicate rule name: {rule.name!r}")
        seen_names.add(rule.name)
        rules.append(rule)

    profile_hash = _canonical_hash(name, version, default.value,
                                     [_rule_dict(r) for r in rules])
    return RulesProfile(
        name=name,
        version=version,
        rules=tuple(rules),
        default=default,
        profile_hash=profile_hash,
        raw=raw,
    )


def _rule_from_dict(r: dict) -> Rule:
    if not isinstance(r, dict):
        raise RulesError(f"rule must be a mapping, got {type(r).__name__}")
    name = r.get("name")
    verdict_str = r.get("verdict")
    conds_raw = r.get("conditions", [])
    if not isinstance(name, str) or not name.strip():
        raise RulesError("rule 'name' must be a non-empty string")
    try:
        verdict = Verdict(verdict_str)
    except ValueError:
        raise RulesError(
            f"rule {name!r}: invalid verdict {verdict_str!r}"
        ) from None
    if not isinstance(conds_raw, list):
        raise RulesError(f"rule {name!r}: conditions must be a list")

    conds: list[Condition] = []
    for c in conds_raw:
        conds.append(_condition_from_dict(name, c))

    return Rule(name=name, conditions=tuple(conds), verdict=verdict)


def _condition_from_dict(rule_name: str, c: dict) -> Condition:
    if not isinstance(c, dict):
        raise RulesError(f"rule {rule_name!r}: condition must be a mapping")
    field = c.get("field")
    op = c.get("op")
    value = c.get("value")
    if field not in _ALLOWED_FIELDS:
        raise RulesError(
            f"rule {rule_name!r}: field {field!r} not in {sorted(_ALLOWED_FIELDS)}"
        )
    if op not in _OPERATORS:
        raise RulesError(
            f"rule {rule_name!r}: operator {op!r} not in {sorted(_OPERATORS)}"
        )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RulesError(
            f"rule {rule_name!r}: value must be numeric, got {value!r}"
        )
    return Condition(field=field, op=op, value=float(value))


def _rule_dict(r: Rule) -> dict:
    return {
        "name": r.name,
        "verdict": r.verdict.value,
        "conditions": [
            {"field": c.field, "op": c.op, "value": c.value}
            for c in r.conditions
        ],
    }


def _canonical_hash(name: str, version: int, default: str,
                     rules_dicts: list[dict]) -> str:
    payload = json.dumps({
        "name": name, "version": version,
        "default": default, "rules": rules_dicts,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate(
    profile: RulesProfile,
    *,
    fit_score: float,
    intrinsic_score: float,
    blocking_gap_count: int,
) -> Decision:
    """
    Apply rules in order; first match wins. Return the profile's
    default verdict if no rule matches.
    """
    facts = {
        "fit_score": fit_score,
        "intrinsic_score": intrinsic_score,
        "blocking_gap_count": blocking_gap_count,
    }
    for rule in profile.rules:
        if rule.matches(facts):
            return Decision(
                verdict=rule.verdict,
                rule_name=rule.name,
                reason=(
                    f"matched rule {rule.name!r}: "
                    f"fit_score={fit_score:.3f} "
                    f"intrinsic_score={intrinsic_score:.3f} "
                    f"blocking_gaps={blocking_gap_count}"
                ),
            )
    return Decision(
        verdict=profile.default,
        rule_name="__default__",
        reason=(
            f"no rule matched; default verdict {profile.default.value}. "
            f"fit_score={fit_score:.3f} intrinsic_score={intrinsic_score:.3f} "
            f"blocking_gaps={blocking_gap_count}"
        ),
    )
