"""
Recommendation orchestrator — Step 12.

One entry point: recommend_for_requirement(). Steps:
  1. Load the requirement + constraints from DB.
  2. Find candidate capability_versions (any with a scorecard).
  3. For each candidate, load evidence and call evaluate_fit — the
     pure fit computation. Persist a fit_evaluation + fit_gap +
     fit_evidence_link rows.
  4. Rank candidates by (blocking_gaps ASC, fit_score DESC,
     intrinsic_score DESC, id ASC).
  5. Apply the rules profile to the top-ranked candidate → verdict.
     If no candidates: verdict = BUILD (deferred to Step 13's search-
     before-build gate for authorization).
  6. Persist recommendation + recommendation_candidate rows.

Contract with schema:
  recommendation_pin_required forces every non-BUILD/REJECT verdict to
  carry a chosen_capability_version_id AND pinned_revision_key AND
  pinned_source_asset_key. The orchestrator populates all three when
  it picks a winner; the CHECK stops us shipping a partial row.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg

from core.project.fit import evaluate_fit
from core.project.rules import RulesProfile, evaluate, load as load_rules
from core.project.types import (
    CandidateInputs,
    Constraint,
    DependencyEvidence,
    FitResult,
    InterfaceEvidence,
    LicenseEvidence,
    Requirement,
    ScoredCandidate,
    Verdict,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecommendationOutcome:
    recommendation_id: uuid.UUID
    project_requirement_id: uuid.UUID
    verdict: Verdict
    rule_name: str
    chosen_capability_version_id: uuid.UUID | None
    pinned_revision_key: str | None
    pinned_source_asset_key: str | None
    candidate_count: int


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def recommend_for_requirement(
    conn: psycopg.Connection,
    *,
    project_requirement_id: uuid.UUID,
    rules_profile: RulesProfile,
    candidate_capability_version_ids: list[uuid.UUID] | None = None,
) -> RecommendationOutcome:
    """
    Compute + persist a recommendation for one requirement.

    If candidate_capability_version_ids is None, we consider every
    capability_version with at least one scorecard row. Callers can
    pass a narrower set for tests or for targeted re-recommendation.
    """
    requirement = _load_requirement(conn, project_requirement_id)
    profile_id = _upsert_rules_profile(conn, rules_profile)

    if candidate_capability_version_ids is None:
        candidate_ids = _find_candidate_ids(conn)
    else:
        candidate_ids = list(candidate_capability_version_ids)

    scored: list[ScoredCandidate] = []
    fit_id_by_cv: dict[uuid.UUID, uuid.UUID] = {}
    for cv_id in candidate_ids:
        inputs = _load_candidate_inputs(conn, cv_id)
        if inputs is None:
            continue                             # no scorecard = not a candidate
        fit_result = evaluate_fit(requirement, inputs)
        fit_id = _persist_fit_evaluation(
            conn,
            project_requirement_id=project_requirement_id,
            candidate=inputs,
            fit_result=fit_result,
        )
        fit_id_by_cv[cv_id] = fit_id
        scored.append(ScoredCandidate(inputs=inputs, fit_result=fit_result))

    # Rank.
    ranked = sorted(
        scored,
        key=lambda s: (
            s.fit_result.blocking_gap_count,       # fewer is better
            -s.fit_result.fit_score,               # higher is better
            -s.inputs.intrinsic_score,             # higher is better
            s.inputs.capability_version_id,        # stable tie-break
        ),
    )

    # Decide.
    if not ranked:
        # No candidates at all → BUILD (needs Step 13 authorization).
        decision = evaluate(
            rules_profile,
            fit_score=0.0,
            intrinsic_score=0.0,
            blocking_gap_count=0,
        )
        # Force BUILD regardless of rules — with no candidates, nothing
        # else makes sense. The rule engine's default is also BUILD, so
        # this is a defensive alignment.
        from core.project.types import Decision
        decision = Decision(
            verdict=Verdict.BUILD,
            rule_name="__no_candidates__",
            reason="no candidate capability_versions found for requirement",
        )
        winner = None
    else:
        winner = ranked[0]
        decision = evaluate(
            rules_profile,
            fit_score=winner.fit_result.fit_score,
            intrinsic_score=winner.inputs.intrinsic_score,
            blocking_gap_count=winner.fit_result.blocking_gap_count,
        )

    # Persist recommendation + candidates.
    rec_id = _persist_recommendation(
        conn,
        project_requirement_id=project_requirement_id,
        profile_id=profile_id,
        decision=decision,
        winner=winner,
    )
    _persist_recommendation_candidates(
        conn,
        recommendation_id=rec_id,
        ranked=ranked,
        fit_id_by_cv=fit_id_by_cv,
    )
    conn.commit()

    return RecommendationOutcome(
        recommendation_id=rec_id,
        project_requirement_id=project_requirement_id,
        verdict=decision.verdict,
        rule_name=decision.rule_name,
        chosen_capability_version_id=(
            uuid.UUID(winner.inputs.capability_version_id)
            if (winner and decision.verdict not in (Verdict.REJECT, Verdict.BUILD))
            else None
        ),
        pinned_revision_key=(
            winner.inputs.pinned_revision_key
            if (winner and decision.verdict not in (Verdict.REJECT, Verdict.BUILD))
            else None
        ),
        pinned_source_asset_key=(
            winner.inputs.pinned_source_asset_key
            if (winner and decision.verdict not in (Verdict.REJECT, Verdict.BUILD))
            else None
        ),
        candidate_count=len(ranked),
    )


# ---------------------------------------------------------------------------
# DB reads
# ---------------------------------------------------------------------------

def _load_requirement(conn, req_id: uuid.UUID) -> Requirement:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, slug, description FROM project_requirement "
            "WHERE id = %s",
            (str(req_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"project_requirement {req_id} not found")
        cur.execute(
            "SELECT kind, detail FROM requirement_constraint "
            "WHERE project_requirement_id = %s ORDER BY id",
            (str(req_id),),
        )
        cons = [
            Constraint(kind=r[0], detail=r[1]) for r in cur.fetchall()
        ]
    return Requirement(
        id=str(row[0]), slug=row[1], description=row[2],
        constraints=tuple(cons),
    )


def _find_candidate_ids(conn) -> list[uuid.UUID]:
    """Any capability_version that has a scorecard is a candidate."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT sc.capability_version_id
            FROM scorecard sc
            ORDER BY sc.capability_version_id
            """
        )
        return [r[0] for r in cur.fetchall()]


def _load_candidate_inputs(
    conn, cv_id: uuid.UUID
) -> CandidateInputs | None:
    """Assemble everything the pure fit function needs."""
    with conn.cursor() as cur:
        # intrinsic_score — best scorecard for this version.
        cur.execute(
            "SELECT COALESCE(MAX(total_score), 0.0) FROM scorecard "
            "WHERE capability_version_id = %s",
            (str(cv_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        intrinsic_score = float(row[0])

        # Pinned revision + asset — take the most recent source binding.
        cur.execute(
            """
            SELECT sr.revision_key, sa.external_key
            FROM capability_source_binding csb
            JOIN source_revision sr ON sr.id = csb.source_revision_id
            JOIN source_asset sa    ON sa.id = sr.source_asset_id
            WHERE csb.capability_version_id = %s
            ORDER BY sr.identified_at DESC
            LIMIT 1
            """,
            (str(cv_id),),
        )
        binding = cur.fetchone()
        if not binding:
            return None
        pinned_revision_key, pinned_source_asset_key = binding

        # Interfaces.
        cur.execute(
            """
            SELECT evidence_item_id, kind, name,
                   COALESCE(signature, ''), language
            FROM capability_interface
            WHERE capability_version_id = %s
            ORDER BY name
            """,
            (str(cv_id),),
        )
        interfaces = tuple(
            InterfaceEvidence(
                evidence_item_id=str(r[0]),
                kind=r[1], name=r[2], signature=r[3], language=r[4],
            )
            for r in cur.fetchall()
        )

        # Dependencies.
        cur.execute(
            """
            SELECT evidence_item_id, depends_on_ecosystem,
                   depends_on_name, dep_kind
            FROM capability_dependency
            WHERE capability_version_id = %s
            ORDER BY depends_on_ecosystem, depends_on_name
            """,
            (str(cv_id),),
        )
        deps = tuple(
            DependencyEvidence(
                evidence_item_id=str(r[0]),
                ecosystem=r[1], name=r[2], kind=r[3],
            )
            for r in cur.fetchall()
        )

        # Licenses — pulled from evidence_item rows for the bound
        # revision(s). Same pattern as scoring's evidence load.
        cur.execute(
            """
            SELECT ei.id, ei.extracted_value
            FROM evidence_item ei
            JOIN capability_source_binding csb
              ON csb.source_revision_id = ei.source_revision_id
            WHERE csb.capability_version_id = %s
              AND ei.evidence_type = 'license'
            ORDER BY ei.id
            """,
            (str(cv_id),),
        )
        licenses = tuple(
            LicenseEvidence(
                evidence_item_id=str(r[0]),
                spdx_id=r[1].get("spdx_id", "unknown"),
            )
            for r in cur.fetchall()
        )

    return CandidateInputs(
        capability_version_id=str(cv_id),
        intrinsic_score=intrinsic_score,
        interfaces=interfaces,
        dependencies=deps,
        licenses=licenses,
        pinned_revision_key=pinned_revision_key,
        pinned_source_asset_key=pinned_source_asset_key,
    )


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _upsert_rules_profile(conn, profile: RulesProfile) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, profile_hash FROM recommendation_rules_profile "
            "WHERE name = %s AND version = %s",
            (profile.name, profile.version),
        )
        row = cur.fetchone()
    if row is not None:
        existing_id, existing_hash = row
        if existing_hash != profile.profile_hash:
            raise ValueError(
                f"recommendation_rules_profile {profile.name!r} "
                f"v{profile.version} is already registered with a different "
                f"hash — profile versions are immutable, bump the version"
            )
        return existing_id

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recommendation_rules_profile
              (name, version, profile_hash, rules)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (profile.name, profile.version, profile.profile_hash,
             json.dumps(profile.raw.get("rules", []))),
        )
        return cur.fetchone()[0]


def _persist_fit_evaluation(
    conn,
    *,
    project_requirement_id: uuid.UUID,
    candidate: CandidateInputs,
    fit_result: FitResult,
) -> uuid.UUID:
    """
    Insert-or-replace at the fit_evaluation level. On conflict we wipe
    the old fit_gap / fit_evidence_link rows (CASCADE) and rewrite —
    fit is a derived view like scorecard.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fit_evaluation
              (project_requirement_id, capability_version_id,
               fit_score, blocking_gap_count, computed_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_requirement_id, capability_version_id)
              DO UPDATE SET fit_score = EXCLUDED.fit_score,
                            blocking_gap_count = EXCLUDED.blocking_gap_count,
                            computed_hash = EXCLUDED.computed_hash,
                            computed_at = now()
            RETURNING id
            """,
            (
                str(project_requirement_id),
                candidate.capability_version_id,
                fit_result.fit_score,
                fit_result.blocking_gap_count,
                fit_result.computed_hash,
            ),
        )
        fit_id = cur.fetchone()[0]

        # Wipe prior gap + evidence rows (CASCADE takes evidence links).
        cur.execute("DELETE FROM fit_gap WHERE fit_evaluation_id = %s",
                    (str(fit_id),))
        cur.execute(
            "DELETE FROM fit_evidence_link WHERE fit_evaluation_id = %s",
            (str(fit_id),),
        )

        for gap in fit_result.gaps:
            cur.execute(
                """
                INSERT INTO fit_gap
                  (fit_evaluation_id, kind, is_blocking, detail)
                VALUES (%s, %s, %s, %s::jsonb)
                """,
                (str(fit_id), gap.kind, gap.is_blocking,
                 json.dumps(gap.detail)),
            )
        for link in fit_result.evidence_links:
            cur.execute(
                """
                INSERT INTO fit_evidence_link
                  (fit_evaluation_id, evidence_item_id, role)
                VALUES (%s, %s, %s)
                """,
                (str(fit_id), link.evidence_item_id, link.role),
            )

    return fit_id


def _persist_recommendation(
    conn,
    *,
    project_requirement_id: uuid.UUID,
    profile_id: uuid.UUID,
    decision,
    winner: ScoredCandidate | None,
) -> uuid.UUID:
    with conn.cursor() as cur:
        # A prior recommendation for this requirement gets replaced. The
        # UNIQUE constraint on project_requirement_id would otherwise
        # block a re-run.
        cur.execute(
            "DELETE FROM recommendation WHERE project_requirement_id = %s",
            (str(project_requirement_id),),
        )

        pin_cv = None
        pin_rev = None
        pin_asset = None
        if winner is not None and decision.verdict not in (
            Verdict.REJECT, Verdict.BUILD
        ):
            pin_cv = winner.inputs.capability_version_id
            pin_rev = winner.inputs.pinned_revision_key
            pin_asset = winner.inputs.pinned_source_asset_key

        cur.execute(
            """
            INSERT INTO recommendation
              (project_requirement_id, rules_profile_id, verdict,
               chosen_capability_version_id, pinned_revision_key,
               pinned_source_asset_key, rule_name, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(project_requirement_id), str(profile_id),
                decision.verdict.value,
                pin_cv, pin_rev, pin_asset,
                decision.rule_name, decision.reason,
            ),
        )
        return cur.fetchone()[0]


def _persist_recommendation_candidates(
    conn,
    *,
    recommendation_id: uuid.UUID,
    ranked: list[ScoredCandidate],
    fit_id_by_cv: dict[uuid.UUID, uuid.UUID],
) -> None:
    with conn.cursor() as cur:
        for i, sc in enumerate(ranked, start=1):
            cv_id = sc.inputs.capability_version_id
            fit_id = fit_id_by_cv[uuid.UUID(cv_id)]
            cur.execute(
                """
                INSERT INTO recommendation_candidate
                  (recommendation_id, capability_version_id,
                   fit_evaluation_id, rank)
                VALUES (%s, %s, %s, %s)
                """,
                (str(recommendation_id), cv_id, str(fit_id), i),
            )


# ---------------------------------------------------------------------------
# Convenience: load the default rules profile from disk
# ---------------------------------------------------------------------------

def load_default_rules() -> RulesProfile:
    return load_rules(
        Path(__file__).parent.parent / "config" /
        "recommendation_rules" / "default.yaml"
    )
