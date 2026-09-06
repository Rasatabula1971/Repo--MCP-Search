"""
Search-before-build gate — Step 13.

The rule the whole platform exists to enforce: no code generation for a
requirement without a prior documented search + evaluation for it.
Refusal is done by this module (the workflow), not by convention.

Two entry points:
  1. authorize_build(recommendation_id, authorized_by, reason)
     Creates a build_authorization row. Refuses if:
       - the recommendation doesn't exist
       - the recommendation's verdict isn't BUILD
       - the recommendation lacks search evidence (see _has_documented_
         search below)
       - actor or reason are empty
  2. can_generate_for(project_requirement_id)
     Returns a BuildDecision. allowed=True only if the latest
     recommendation for the requirement is BUILD AND has been authorized.

"Documented search" means one of:
  - The recommendation has at least one recommendation_candidate row
    (candidates were evaluated).
  - The recommendation's rule_name is '__no_candidates__' (a search
    happened but nothing was found).
A recommendation with neither is a fake — someone inserted it via raw
SQL without running the orchestrator — and authorize_build refuses.

An authorization is per-recommendation. If the recommendation is
re-run (recommend_for_requirement DELETEs+INSERTs), the FK CASCADE
takes the old build_authorization row too, and the new recommendation
must be authorized separately. That's what makes "one authorization,
one search" a real relationship.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BuildGateError(Exception):
    """Base for search-before-build gate refusals."""


class RecommendationNotFound(BuildGateError):
    pass


class NotABuildVerdict(BuildGateError):
    """The recommendation exists but its verdict isn't BUILD."""


class MissingSearchEvidence(BuildGateError):
    """
    The recommendation is BUILD but has neither candidate rows nor the
    __no_candidates__ sentinel — no proof a search actually happened.
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BuildDecision:
    allowed: bool
    reason: str
    recommendation_id: uuid.UUID | None
    build_authorization_id: uuid.UUID | None


# ---------------------------------------------------------------------------
# authorize_build — the workflow refusal
# ---------------------------------------------------------------------------

def authorize_build(
    conn: psycopg.Connection,
    *,
    recommendation_id: uuid.UUID,
    authorized_by: str,
    reason: str,
    metadata: dict | None = None,
) -> uuid.UUID:
    """
    Create a build_authorization row for a BUILD recommendation.

    Refuses (raises) if:
      - the recommendation doesn't exist
      - the recommendation's verdict isn't BUILD
      - the recommendation lacks search evidence (see module docstring)
      - actor or reason is empty (Python layer; DB CHECK is a backstop)

    Idempotent: if an authorization already exists for the recommendation,
    returns its id.
    """
    if not authorized_by.strip():
        raise ValueError("authorized_by must be non-empty")
    if not reason.strip():
        raise ValueError("reason must be non-empty — audit trail requires it")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, rule_name FROM recommendation WHERE id = %s",
            (str(recommendation_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise RecommendationNotFound(
            f"recommendation {recommendation_id} not found"
        )
    verdict, rule_name = row
    if verdict != "BUILD":
        raise NotABuildVerdict(
            f"recommendation {recommendation_id} verdict is {verdict!r}, "
            f"not BUILD — nothing to authorize for code generation"
        )

    if not _has_documented_search(conn, recommendation_id, rule_name):
        raise MissingSearchEvidence(
            f"recommendation {recommendation_id} is BUILD but has neither "
            f"recommendation_candidate rows nor rule_name='__no_candidates__' "
            f"— no proof a real search happened. Re-run "
            f"recommend_for_requirement or explain the missing evidence."
        )

    # Idempotency: return existing authorization if present.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM build_authorization WHERE recommendation_id = %s",
            (str(recommendation_id),),
        )
        existing = cur.fetchone()
    if existing:
        return existing[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO build_authorization
              (recommendation_id, authorized_by, reason, metadata)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (str(recommendation_id), authorized_by.strip(), reason.strip(),
             json.dumps(metadata or {})),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# can_generate_for — the gate any code generator must call first
# ---------------------------------------------------------------------------

def can_generate_for(
    conn: psycopg.Connection,
    *,
    project_requirement_id: uuid.UUID,
) -> BuildDecision:
    """
    Decide whether code generation is authorized for a requirement.

    Returns allowed=True only when:
      - a recommendation exists for the requirement
      - its verdict is BUILD
      - a build_authorization row references it

    Everything else — no recommendation, non-BUILD verdict, BUILD without
    authorization — returns allowed=False with a specific reason.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, verdict FROM recommendation "
            "WHERE project_requirement_id = %s",
            (str(project_requirement_id),),
        )
        row = cur.fetchone()

    if row is None:
        return BuildDecision(
            allowed=False,
            reason=(
                "no recommendation exists for this requirement — "
                "run recommend_for_requirement first"
            ),
            recommendation_id=None,
            build_authorization_id=None,
        )
    rec_id, verdict = row
    if verdict != "BUILD":
        return BuildDecision(
            allowed=False,
            reason=(
                f"recommendation verdict is {verdict!r}, not BUILD — "
                f"an existing capability was recommended; use it instead"
            ),
            recommendation_id=rec_id,
            build_authorization_id=None,
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM build_authorization WHERE recommendation_id = %s",
            (str(rec_id),),
        )
        auth_row = cur.fetchone()

    if auth_row is None:
        return BuildDecision(
            allowed=False,
            reason=(
                "BUILD verdict exists but is not authorized — call "
                "authorize_build() with actor + reason before generating"
            ),
            recommendation_id=rec_id,
            build_authorization_id=None,
        )

    return BuildDecision(
        allowed=True,
        reason="BUILD verdict has been explicitly authorized",
        recommendation_id=rec_id,
        build_authorization_id=auth_row[0],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEARCH_SENTINEL_RULES = frozenset({
    "__no_candidates__",   # orchestrator found zero candidates
})


def _has_documented_search(
    conn: psycopg.Connection,
    recommendation_id: uuid.UUID,
    rule_name: str,
) -> bool:
    """
    Proof-of-search check. A recommendation demonstrates a search
    happened if EITHER:
      - it has recommendation_candidate rows (candidates were evaluated), OR
      - its rule_name is one of the orchestrator's sentinels signalling
        a real search that found nothing.
    """
    if rule_name in _SEARCH_SENTINEL_RULES:
        return True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM recommendation_candidate "
            "WHERE recommendation_id = %s",
            (str(recommendation_id),),
        )
        return cur.fetchone()[0] > 0
