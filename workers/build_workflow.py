"""
Build workflow — Step 13.

A single entry point, `generate_implementation`, that any hypothetical
code generator must go through. It calls the search-before-build gate
and refuses if generation isn't authorized.

This module is deliberately minimal — the actual code generation is
beyond this MVP slice. What matters here is that the refusal is
structural: no code path reaches "would generate code" without the
gate having said yes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from core.policy.build_gate import (
    BuildDecision,
    BuildGateError,
    can_generate_for,
)


class BuildNotAuthorized(BuildGateError):
    """
    Raised by generate_implementation when the gate refuses.
    Carries the BuildDecision so callers can inspect why.
    """
    def __init__(self, decision: BuildDecision):
        self.decision = decision
        super().__init__(decision.reason)


@dataclass(frozen=True)
class GenerationOutcome:
    """
    A stub for what a real generator would produce. In this MVP we do
    not generate code — the workflow just proves it CAN be authorized
    and would be safely gated when a real generator is bolted on.
    """
    project_requirement_id: uuid.UUID
    build_authorization_id: uuid.UUID
    would_generate: bool


def generate_implementation(
    conn: psycopg.Connection,
    *,
    project_requirement_id: uuid.UUID,
) -> GenerationOutcome:
    """
    The (currently stubbed) code-generation entry point. Every real
    generator hangs off of this — the gate check is the first thing
    that happens, and refusal raises BuildNotAuthorized.

    Returns GenerationOutcome with would_generate=True on success. In
    a future step this becomes an actual generation call; the gate
    contract stays the same.
    """
    decision = can_generate_for(
        conn, project_requirement_id=project_requirement_id
    )
    if not decision.allowed:
        raise BuildNotAuthorized(decision)

    # In a real workflow: dispatch to a generator here. For MVP, we
    # return the outcome that proves the gate approved.
    return GenerationOutcome(
        project_requirement_id=project_requirement_id,
        build_authorization_id=decision.build_authorization_id,
        would_generate=True,
    )
