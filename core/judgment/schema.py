"""
Judgment schema — Step 7.

The four fields every provider response must carry:

    verdict:         short string identifying the model's decision
    criteria_scores: dict[str, float in 0..1] — per-dimension scores
    self_confidence: float in 0..1 — the model's own confidence
    evidence_refs:   list of evidence_item ids the response cites

Schema validation is DETERMINISTIC. A malformed response is rejected by
this module, in-process — no second model call is spent trying to figure
out what went wrong. That's what makes "provider-reliability evidence"
a real signal: schema_valid=false on many responses from a provider is
a fact we can act on.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Judgment:
    """A validated judgment payload. Only constructed via parse()."""
    verdict: str
    criteria_scores: dict[str, float]
    self_confidence: float
    evidence_refs: list[str]           # stored as strings; UUID validated

    def to_json(self) -> str:
        return json.dumps({
            "verdict": self.verdict,
            "criteria_scores": self.criteria_scores,
            "self_confidence": self.self_confidence,
            "evidence_refs": self.evidence_refs,
        })


class SchemaError(ValueError):
    """
    Raised when a provider response fails schema validation. Caller
    catches this once, persists the failure to judgment_response with
    schema_valid=false, and does NOT retry the model call.
    """


def parse(raw: str) -> Judgment:
    """
    Parse and validate a raw provider response. On any failure raises
    SchemaError with a human-readable reason.

    Strict about types — a self_confidence of "0.8" (string) is rejected,
    not silently coerced. If a provider is returning types we didn't
    expect, we want that visible in the reliability metrics.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"response is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SchemaError(
            f"top-level response must be an object, got {type(data).__name__}"
        )

    required = ("verdict", "criteria_scores", "self_confidence", "evidence_refs")
    missing = [k for k in required if k not in data]
    if missing:
        raise SchemaError(f"missing required fields: {missing}")

    verdict = data["verdict"]
    if not isinstance(verdict, str) or not verdict.strip():
        raise SchemaError("verdict must be a non-empty string")

    criteria_scores = data["criteria_scores"]
    if not isinstance(criteria_scores, dict) or not criteria_scores:
        raise SchemaError(
            "criteria_scores must be a non-empty object of "
            "{dimension: score}"
        )
    for dim, score in criteria_scores.items():
        if not isinstance(dim, str):
            raise SchemaError(f"criteria dimension must be string, got {dim!r}")
        if not _is_score(score):
            raise SchemaError(
                f"criteria score for {dim!r} must be a number in [0, 1], "
                f"got {score!r}"
            )

    self_conf = data["self_confidence"]
    if not _is_score(self_conf):
        raise SchemaError(
            f"self_confidence must be a number in [0, 1], got {self_conf!r}"
        )

    evidence_refs = data["evidence_refs"]
    if not isinstance(evidence_refs, list):
        raise SchemaError("evidence_refs must be a list")
    validated_refs: list[str] = []
    for ref in evidence_refs:
        if not isinstance(ref, str):
            raise SchemaError(
                f"evidence_ref must be a string UUID, got {ref!r}"
            )
        try:
            uuid.UUID(ref)
        except ValueError:
            raise SchemaError(
                f"evidence_ref {ref!r} is not a valid UUID"
            ) from None
        validated_refs.append(ref)

    return Judgment(
        verdict=verdict,
        criteria_scores={k: float(v) for k, v in criteria_scores.items()},
        self_confidence=float(self_conf),
        evidence_refs=validated_refs,
    )


def _is_score(x: Any) -> bool:
    """True iff x is an int/float in [0, 1]. Rejects bool (which is
    technically int in Python but not a meaningful score)."""
    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    return 0.0 <= float(x) <= 1.0
