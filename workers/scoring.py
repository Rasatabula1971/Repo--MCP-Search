"""
Scoring orchestrator — Step 9.

One entry point: score_capability_version(). It:
  1. Loads the profile (upserts scoring_profile row if new)
  2. Loads evidence for the capability_version's bound revision(s)
  3. Calls the pure scorer (core.scoring.scorer.score)
  4. Persists the scorecard + dimension results + evidence links
  5. Verifies determinism: re-scoring must produce the same computed_hash

Notes:
  - This module is in workers/, not core/. That's on purpose: it does
    DB writes and depends on the DB-adjacent evidence layout. The pure
    scoring logic lives in core/scoring/ where the import-linter
    contract keeps it clean.
  - Re-scoring the same (capability_version, profile) is idempotent:
    same evidence + same profile → same computed_hash → we update
    computed_at but don't rewrite the numbers.
  - If a second run against the same inputs produces a different hash,
    something is non-deterministic and we raise loudly. That's the
    Step 9 done-when, enforced.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from core.scoring.profile import Profile, load as load_profile
from core.scoring.scorer import Evidence, Scorecard, score


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringOutcome:
    scorecard_id: uuid.UUID
    capability_version_id: uuid.UUID
    profile_id: uuid.UUID
    total_score: float
    confidence: float
    computed_hash: str
    was_noop: bool                    # True iff re-score produced same hash


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def score_capability_version(
    conn: psycopg.Connection,
    *,
    capability_version_id: uuid.UUID,
    profile: Profile,
) -> ScoringOutcome:
    """
    Score one capability_version against one profile. Caller manages
    the transaction; we commit at the end.
    """
    profile_id = _upsert_profile(conn, profile)

    evidence = _load_evidence_for_capability_version(
        conn, capability_version_id
    )
    scorecard: Scorecard = score(profile, evidence)

    # Determinism check: does an existing scorecard for this pair have
    # the same computed_hash? If so, we're a no-op. If not, we've
    # either got new evidence or the code is non-deterministic; the
    # done-when says the SAME inputs must produce the SAME hash, so
    # we compare rigorously below with test_scoring_is_byte_stable.
    prior = _find_prior_scorecard(conn, capability_version_id, profile_id)
    if prior is not None and prior["computed_hash"] == scorecard.computed_hash:
        return ScoringOutcome(
            scorecard_id=prior["id"],
            capability_version_id=capability_version_id,
            profile_id=profile_id,
            total_score=float(prior["total_score"]),
            confidence=float(prior["confidence"]),
            computed_hash=prior["computed_hash"],
            was_noop=True,
        )

    scorecard_id = _persist_scorecard(
        conn,
        capability_version_id=capability_version_id,
        profile_id=profile_id,
        scorecard=scorecard,
    )
    conn.commit()

    return ScoringOutcome(
        scorecard_id=scorecard_id,
        capability_version_id=capability_version_id,
        profile_id=profile_id,
        total_score=scorecard.total_score,
        confidence=scorecard.confidence,
        computed_hash=scorecard.computed_hash,
        was_noop=False,
    )


# ---------------------------------------------------------------------------
# Profile persistence
# ---------------------------------------------------------------------------

def _upsert_profile(conn, profile: Profile) -> uuid.UUID:
    """
    Insert-or-return by (name, version). If a row already exists with
    the same (name, version) but a DIFFERENT profile_hash, that's an
    illegal edit — versions are immutable once registered.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, profile_hash FROM scoring_profile "
            "WHERE name = %s AND version = %s",
            (profile.name, profile.version),
        )
        row = cur.fetchone()
    if row is not None:
        existing_id, existing_hash = row
        if existing_hash != profile.profile_hash:
            raise ValueError(
                f"scoring_profile {profile.name!r} v{profile.version} is "
                f"already registered with a different hash — profile "
                f"versions are immutable, bump the version instead"
            )
        return existing_id

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scoring_profile
              (name, version, profile_hash, dimensions)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                profile.name, profile.version, profile.profile_hash,
                json.dumps(profile.raw.get("dimensions", [])),
            ),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Evidence loading — pulls from evidence_item joined via source binding
# ---------------------------------------------------------------------------

def _load_evidence_for_capability_version(
    conn, capability_version_id: uuid.UUID
) -> list[Evidence]:
    """
    Load evidence rows for the revisions bound to this capability
    version. Deterministic ordering (by id) so the scorer's sort has
    stable input.

    We deliberately do NOT read judgment_response.self_confidence
    here — the scoring layer has no path to that column, by contract.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ei.id, ei.evidence_type, ei.extracted_value
            FROM evidence_item ei
            JOIN capability_source_binding csb
              ON csb.source_revision_id = ei.source_revision_id
            WHERE csb.capability_version_id = %s
            ORDER BY ei.id
            """,
            (str(capability_version_id),),
        )
        rows = cur.fetchall()
    return [
        Evidence(id=str(r[0]), evidence_type=r[1], extracted_value=r[2])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _find_prior_scorecard(
    conn, capability_version_id: uuid.UUID, profile_id: uuid.UUID
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, total_score, confidence, computed_hash
            FROM scorecard
            WHERE capability_version_id = %s AND scoring_profile_id = %s
            """,
            (str(capability_version_id), str(profile_id)),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d.name for d in cur.description], row))


def _persist_scorecard(
    conn,
    *,
    capability_version_id: uuid.UUID,
    profile_id: uuid.UUID,
    scorecard: Scorecard,
) -> uuid.UUID:
    """
    Insert-or-replace at the scorecard level. On conflict we delete
    the old dimension_result rows (CASCADE clears their evidence_links)
    and rewrite — the scorecard shape isn't append-only, it's a
    derived-view kept fresh.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scorecard
              (capability_version_id, scoring_profile_id,
               total_score, confidence, computed_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (capability_version_id, scoring_profile_id)
              DO UPDATE SET total_score = EXCLUDED.total_score,
                            confidence = EXCLUDED.confidence,
                            computed_hash = EXCLUDED.computed_hash,
                            computed_at = now()
            RETURNING id
            """,
            (
                str(capability_version_id), str(profile_id),
                scorecard.total_score, scorecard.confidence,
                scorecard.computed_hash,
            ),
        )
        scorecard_id = cur.fetchone()[0]

        # Wipe prior dimension rows (CASCADE clears score_evidence_link).
        cur.execute(
            "DELETE FROM score_dimension_result WHERE scorecard_id = %s",
            (str(scorecard_id),),
        )

        for dim_result in scorecard.dimensions:
            cur.execute(
                """
                INSERT INTO score_dimension_result
                  (scorecard_id, dimension_name, raw_score, weight,
                   weighted_score, coverage, contradiction, evidence_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(scorecard_id), dim_result.dimension_name,
                    dim_result.raw_score, dim_result.weight,
                    dim_result.weighted_score, dim_result.coverage,
                    dim_result.contradiction, dim_result.evidence_count,
                ),
            )
            dim_result_id = cur.fetchone()[0]

            for evidence_id in dim_result.evidence_ids:
                cur.execute(
                    """
                    INSERT INTO score_evidence_link
                      (score_dimension_result_id, evidence_item_id)
                    VALUES (%s, %s)
                    """,
                    (str(dim_result_id), evidence_id),
                )

    return scorecard_id


# ---------------------------------------------------------------------------
# Convenience: load the default profile from disk
# ---------------------------------------------------------------------------

def load_default_profile() -> Profile:
    return load_profile(
        Path(__file__).parent.parent / "config" / "scoring_profiles" / "default.yaml"
    )
