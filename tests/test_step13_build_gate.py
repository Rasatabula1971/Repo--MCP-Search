"""
Step 13 done-when:
  "Attempting to generate before evaluating is refused by the workflow
   itself, not by convention."

Refusal happens in code — three layers:
  1. workers.build_workflow.generate_implementation → raises
     BuildNotAuthorized if the gate says no.
  2. core.policy.build_gate.can_generate_for → returns
     BuildDecision(allowed=False, reason=...) with a specific reason.
  3. core.policy.build_gate.authorize_build → raises
     NotABuildVerdict / MissingSearchEvidence / RecommendationNotFound
     for the four disqualifying cases.

Test coverage:
  - can_generate_for refuses when no recommendation exists
  - can_generate_for refuses when the recommendation isn't BUILD
  - can_generate_for refuses when BUILD but not authorized
  - can_generate_for allows only after authorize_build succeeds
  - authorize_build refuses non-BUILD verdicts
  - authorize_build refuses BUILD verdicts without search evidence
  - authorize_build requires non-empty actor + reason (Python + DB CHECK)
  - authorize_build is idempotent per recommendation
  - build_authorization is append-only (rules block UPDATE/DELETE)
  - generate_implementation raises BuildNotAuthorized without setup
  - Re-running recommend_for_requirement DELETEs the old rec via UNIQUE
    constraint, which CASCADEs to build_authorization — meaning the new
    recommendation must be authorized separately
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.registry import promote_revision
from core.policy.build_gate import (
    BuildDecision,
    MissingSearchEvidence,
    NotABuildVerdict,
    RecommendationNotFound,
    authorize_build,
    can_generate_for,
)
from core.project.types import Verdict
from workers.analysis import run_analysis
from workers.build_workflow import (
    BuildNotAuthorized,
    generate_implementation,
)
from workers.ingestion import ingest_revision
from workers.recommend import (
    load_default_rules,
    recommend_for_requirement,
)
from workers.scoring import load_default_profile, score_capability_version


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


def _make_requirement(conn, project_name, slug, description, *constraints):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            (project_name,),
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO project_requirement (project_id, slug, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (pid, slug, description),
        )
        rid = cur.fetchone()[0]
        for kind, detail in constraints:
            cur.execute(
                "INSERT INTO requirement_constraint "
                "(project_requirement_id, kind, detail) "
                "VALUES (%s, %s, %s::jsonb)",
                (rid, kind, json.dumps(detail)),
            )
    conn.commit()
    return rid


def _bootstrap_cv(conn, registry, external_key, revision_key, files):
    """Reused from Step 12 pattern: ingest → analyse → promote → score."""
    fake = registry.get("fake")
    fake.add_revision(external_key, revision_key, files)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') "
            "ON CONFLICT (provider_id, external_key) DO UPDATE "
            "SET display_name = EXCLUDED.display_name RETURNING id",
            (pid, external_key, external_key),
        )
        aid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (aid, revision_key),
        )
        rid = cur.fetchone()[0]
    conn.commit()
    ingest_revision(conn, registry=registry, source_revision_id=rid)
    run_analysis(conn, registry=registry, source_revision_id=rid)
    out = promote_revision(
        conn, registry=registry, source_revision_id=rid
    )
    score_capability_version(
        conn,
        capability_version_id=out.capability_version_id,
        profile=load_default_profile(),
    )
    return out.capability_version_id


_STRONG_FILES = {
    "pyproject.toml": (
        b"[project]\nname = 'good-http'\nversion = '1.0.0'\n"
        b"dependencies = ['httpx']\n"
    ),
    "src/good_http/__init__.py": b"",
    "src/good_http/api.py": (
        b"def get(url: str, timeout: int = 30) -> str:\n"
        b"    return url\n"
    ),
    "tests/test_api.py": b"def test_get(): pass\n",
    "LICENSE": (
        b"MIT License\n\n"
        b"Permission is hereby granted, free of charge, to any "
        b"person obtaining a copy\n"
    ),
}


# ---------------------------------------------------------------------------
# can_generate_for — the four states
# ---------------------------------------------------------------------------

def test_can_generate_refuses_when_no_recommendation_exists(conn):
    """A requirement with no recommendation at all → refused."""
    req_id = _make_requirement(conn, "p1", "needs-x", "needs an X")
    decision = can_generate_for(conn, project_requirement_id=req_id)
    assert decision.allowed is False
    assert "no recommendation" in decision.reason
    assert decision.recommendation_id is None
    assert decision.build_authorization_id is None


def test_can_generate_refuses_when_recommendation_is_not_build(conn, registry):
    """A REJECT verdict → refused; user should reject-and-fix, not build."""
    # Weak candidate so recommendation comes out REJECT.
    weak_files = {
        **_STRONG_FILES,
        "pyproject.toml": (
            b"[project]\nname = 'weak'\nversion = '0.1.0'\n"
        ),
        "src/good_http/api.py": b"def unrelated():\n    return 1\n",
    }
    _bootstrap_cv(conn, registry, "acme/weak", "sha", weak_files)
    req_id = _make_requirement(
        conn, "p2", "http-client", "an HTTP client",
        ("required_interface", {"name": "get"}),
        ("required_interface", {"name": "post"}),
        ("required_interface", {"name": "put"}),
    )
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    assert out.verdict == Verdict.REJECT

    decision = can_generate_for(conn, project_requirement_id=req_id)
    assert decision.allowed is False
    assert "not BUILD" in decision.reason
    assert decision.recommendation_id == out.recommendation_id


def test_can_generate_refuses_when_build_but_not_authorized(conn):
    """A valid BUILD verdict without a build_authorization → refused."""
    req_id = _make_requirement(conn, "p3", "no-cands", "needs a thing")
    # No candidates → BUILD via __no_candidates__.
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    assert out.verdict == Verdict.BUILD
    assert out.rule_name == "__no_candidates__"

    decision = can_generate_for(conn, project_requirement_id=req_id)
    assert decision.allowed is False
    assert "not authorized" in decision.reason
    assert decision.recommendation_id == out.recommendation_id
    assert decision.build_authorization_id is None


def test_can_generate_allows_after_authorize_build(conn):
    """The green path: BUILD verdict + authorize_build → allowed."""
    req_id = _make_requirement(conn, "p4", "no-cands", "needs a thing")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    assert out.verdict == Verdict.BUILD

    ba_id = authorize_build(
        conn,
        recommendation_id=out.recommendation_id,
        authorized_by="alice",
        reason="no capability satisfies this requirement per search",
    )
    conn.commit()

    decision = can_generate_for(conn, project_requirement_id=req_id)
    assert decision.allowed is True
    assert decision.recommendation_id == out.recommendation_id
    assert decision.build_authorization_id == ba_id


# ---------------------------------------------------------------------------
# authorize_build — refusal shapes
# ---------------------------------------------------------------------------

def test_authorize_build_refuses_missing_recommendation(conn):
    with pytest.raises(RecommendationNotFound):
        authorize_build(
            conn,
            recommendation_id=uuid.uuid4(),
            authorized_by="alice", reason="test",
        )


def test_authorize_build_refuses_non_build_verdict(conn, registry):
    """A REJECT recommendation cannot be authorized for build."""
    weak_files = {
        **_STRONG_FILES,
        "pyproject.toml": b"[project]\nname = 'weak'\nversion = '0.1'\n",
        "src/good_http/api.py": b"def unrelated(): return 1\n",
    }
    _bootstrap_cv(conn, registry, "acme/weak", "sha", weak_files)
    req_id = _make_requirement(
        conn, "p5", "http-client", "an HTTP client",
        ("required_interface", {"name": "get"}),
        ("required_interface", {"name": "post"}),
        ("required_interface", {"name": "put"}),
    )
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    assert out.verdict == Verdict.REJECT

    with pytest.raises(NotABuildVerdict, match="not BUILD"):
        authorize_build(
            conn, recommendation_id=out.recommendation_id,
            authorized_by="alice", reason="test",
        )


def test_authorize_build_refuses_build_without_search_evidence(conn):
    """
    Belt-and-braces: even if someone bypasses the orchestrator and
    inserts a BUILD recommendation directly via SQL — with no candidate
    rows and no __no_candidates__ sentinel — authorize_build refuses.
    That's the "workflow refusal, not convention" part of the done-when.
    """
    # Build a bogus BUILD recommendation via raw SQL.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO project (name) VALUES ('sneaky') RETURNING id")
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO project_requirement "
            "(project_id, slug, description) VALUES (%s, 's', 'd') "
            "RETURNING id", (pid,),
        )
        rid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO recommendation_rules_profile "
            "(name, version, profile_hash, rules) "
            "VALUES ('sneaky', 1, 'x', '[]'::jsonb) RETURNING id"
        )
        prof_id = cur.fetchone()[0]
        # Note: fabricated rule_name that isn't a valid search sentinel.
        cur.execute(
            """
            INSERT INTO recommendation
              (project_requirement_id, rules_profile_id, verdict,
               rule_name, reason)
            VALUES (%s, %s, 'BUILD', 'i_want_to_build', 'shortcut')
            RETURNING id
            """,
            (str(rid), str(prof_id)),
        )
        bogus_rec_id = cur.fetchone()[0]
    conn.commit()

    with pytest.raises(MissingSearchEvidence, match="no proof"):
        authorize_build(
            conn, recommendation_id=bogus_rec_id,
            authorized_by="alice", reason="test",
        )


def test_authorize_build_accepts_no_candidates_sentinel(conn):
    """
    The __no_candidates__ rule_name IS proof of search — the orchestrator
    only sets it after querying every candidate and finding none.
    """
    req_id = _make_requirement(conn, "p6", "nothing-yet", "needs a thing")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    # Sanity: this specific state — BUILD via no candidates.
    assert out.verdict == Verdict.BUILD
    assert out.rule_name == "__no_candidates__"

    # Authorize succeeds despite zero candidate rows.
    ba_id = authorize_build(
        conn,
        recommendation_id=out.recommendation_id,
        authorized_by="alice",
        reason="genuinely no capability exists yet",
    )
    conn.commit()
    assert ba_id is not None


def test_authorize_build_accepts_when_candidates_exist(conn, registry):
    """
    A BUILD verdict CAN occur even when candidates exist, if the rules
    profile is unusual. Test with a stricter profile that never triggers
    REFERENCE, so a low-fit candidate results in BUILD (via fallthrough).
    Candidates exist → search happened → authorize_build succeeds.
    """
    # Build a strict rules profile: only ADOPT (never REFERENCE/REJECT).
    # A low-fit candidate falls through to default = BUILD.
    from core.project.rules import _from_dict
    strict = _from_dict({
        "name": "strict-adopt-only", "version": 1, "default": "BUILD",
        "rules": [
            {
                "name": "strong_adopt",
                "verdict": "ADOPT",
                "conditions": [
                    {"field": "fit_score", "op": ">=", "value": 0.9},
                    {"field": "intrinsic_score", "op": ">=", "value": 0.7},
                    {"field": "blocking_gap_count", "op": "==", "value": 0},
                ],
            },
        ],
    })

    # Weak candidate: MIT license but wrong interfaces.
    weak_files = {
        **_STRONG_FILES,
        "pyproject.toml": b"[project]\nname = 'weakish'\nversion = '0.1'\n",
        "src/good_http/api.py": b"def unrelated(): return 1\n",
    }
    _bootstrap_cv(conn, registry, "acme/weakish", "sha", weak_files)
    req_id = _make_requirement(
        conn, "p7", "http-client", "an HTTP client",
        ("required_interface", {"name": "get"}),
    )
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=strict,
    )
    assert out.verdict == Verdict.BUILD
    # Candidates DID exist and were evaluated — recommendation_candidate
    # rows are present as proof of search.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM recommendation_candidate "
            "WHERE recommendation_id = %s",
            (str(out.recommendation_id),),
        )
        assert cur.fetchone()[0] >= 1

    # Authorize succeeds — candidate rows are the search evidence.
    ba_id = authorize_build(
        conn, recommendation_id=out.recommendation_id,
        authorized_by="alice",
        reason="evaluated candidates but none met the adopt bar",
    )
    conn.commit()
    assert ba_id is not None


# ---------------------------------------------------------------------------
# authorize_build — audit trail
# ---------------------------------------------------------------------------

def test_authorize_build_requires_non_empty_actor_python_layer(conn):
    req_id = _make_requirement(conn, "p8", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    for empty in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="authorized_by"):
            authorize_build(
                conn, recommendation_id=out.recommendation_id,
                authorized_by=empty, reason="ok",
            )


def test_authorize_build_requires_non_empty_reason_python_layer(conn):
    req_id = _make_requirement(conn, "p9", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    for empty in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="reason"):
            authorize_build(
                conn, recommendation_id=out.recommendation_id,
                authorized_by="alice", reason=empty,
            )


def test_db_check_constraint_blocks_empty_reason(conn):
    """Belt-and-braces: even raw SQL bypassing authorize_build can't
    insert an empty reason."""
    req_id = _make_requirement(conn, "p10", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO build_authorization "
                "(recommendation_id, authorized_by, reason) "
                "VALUES (%s, 'alice', '   ')",
                (str(out.recommendation_id),),
            )


def test_authorize_build_is_idempotent(conn):
    req_id = _make_requirement(conn, "p11", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    first = authorize_build(
        conn, recommendation_id=out.recommendation_id,
        authorized_by="alice", reason="test",
    )
    conn.commit()
    second = authorize_build(
        conn, recommendation_id=out.recommendation_id,
        authorized_by="bob", reason="also test",
    )
    conn.commit()
    assert first == second
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), authorized_by FROM build_authorization "
            "WHERE recommendation_id = %s GROUP BY authorized_by",
            (str(out.recommendation_id),),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "alice"    # original wins; second call was a no-op


def test_build_authorization_is_append_only_for_updates(conn):
    """
    Append-only for UPDATE: no silent rewrites of actor/reason.
    DELETE is intentionally allowed via FK CASCADE (a re-search
    supersedes the old authorization) — see migration 010's comment.
    """
    req_id = _make_requirement(conn, "p12", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    ba_id = authorize_build(
        conn, recommendation_id=out.recommendation_id,
        authorized_by="alice", reason="test",
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE build_authorization SET reason = 'edited' WHERE id = %s",
            (str(ba_id),),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT reason FROM build_authorization WHERE id = %s",
            (str(ba_id),),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "test", "UPDATE should have been silently ignored"


# ---------------------------------------------------------------------------
# generate_implementation — the workflow-level refusal (done-when)
# ---------------------------------------------------------------------------

def test_generate_refuses_without_prior_search_and_evaluation(conn):
    """
    THE done-when: attempting to generate before evaluating is refused
    by the workflow itself. Here the workflow is generate_implementation;
    the refusal is BuildNotAuthorized raised code-level.
    """
    req_id = _make_requirement(conn, "p13", "x", "needs x")
    with pytest.raises(BuildNotAuthorized) as exc:
        generate_implementation(conn, project_requirement_id=req_id)
    assert "no recommendation" in exc.value.decision.reason


def test_generate_refuses_after_search_but_before_authorization(conn):
    """A search happened, verdict is BUILD, but no one authorized it.
    generate_implementation still refuses — an evaluation is not the
    same as an authorization."""
    req_id = _make_requirement(conn, "p14", "x", "needs x")
    recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    with pytest.raises(BuildNotAuthorized) as exc:
        generate_implementation(conn, project_requirement_id=req_id)
    assert "not authorized" in exc.value.decision.reason


def test_generate_succeeds_after_search_and_authorization(conn):
    req_id = _make_requirement(conn, "p15", "x", "needs x")
    out = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    authorize_build(
        conn, recommendation_id=out.recommendation_id,
        authorized_by="alice", reason="genuinely need to build",
    )
    conn.commit()
    result = generate_implementation(conn, project_requirement_id=req_id)
    assert result.would_generate is True
    assert result.project_requirement_id == req_id


# ---------------------------------------------------------------------------
# Re-recommendation invalidates authorization
# ---------------------------------------------------------------------------

def test_new_recommendation_invalidates_prior_authorization(conn):
    """
    Re-running recommend_for_requirement DELETEs the prior recommendation
    row (UNIQUE constraint on project_requirement_id). ON DELETE CASCADE
    on build_authorization means the prior authorization goes with it.
    The new recommendation must be authorized separately.

    This is the mechanism that keeps "one authorization, one search"
    real — you can't authorize once and coast forever.
    """
    req_id = _make_requirement(conn, "p16", "x", "needs x")
    first = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    authorize_build(
        conn, recommendation_id=first.recommendation_id,
        authorized_by="alice", reason="test",
    )
    conn.commit()
    assert can_generate_for(
        conn, project_requirement_id=req_id
    ).allowed is True

    # Re-run — the orchestrator DELETEs the old recommendation row.
    second = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    # Different id — the old one is gone.
    assert second.recommendation_id != first.recommendation_id

    # Old authorization is gone (CASCADE), and the new rec isn't
    # authorized — generation is refused again.
    decision = can_generate_for(conn, project_requirement_id=req_id)
    assert decision.allowed is False
    assert "not authorized" in decision.reason
