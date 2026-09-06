"""
Step 12 done-when:
  "A recommendation names the exact revision to pin, and every material
   claim in it links to evidence."

Test coverage:
  1. Fit computation (pure):
     - required_interface matches by name (case-insensitive) and
       optional signature substring
     - license_allowlist matches, mismatches, missing license
     - forbidden_dependency present vs absent
     - fit_score is deterministic byte-for-byte (same hash on re-run)
     - computed_hash changes when inputs change
  2. Rules interpreter (pure):
     - Loads + hashes + validates YAML
     - Rejects malformed rules (bad field, bad op, bad verdict)
     - First matching rule wins; default fires when none match
     - Duplicate rule names rejected
     - Rules profile is immutable once registered
  3. Orchestrator (integration):
     - Persists fit_evaluation + fit_gap + fit_evidence_link
     - Ranks candidates deterministically
     - Recommendation names an exact revision to pin (Step 12 done-when)
     - Every recommendation's material claims link to evidence rows
     - Fit score and intrinsic score never merged into one column
     - CHECK constraint blocks unpinned non-REJECT/BUILD verdicts
     - BUILD verdict when no candidates
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.registry import promote_revision
from core.project.fit import evaluate_fit
from core.project.rules import (
    RulesError,
    RulesProfile,
    evaluate,
    load as load_rules,
)
from core.project.types import (
    CandidateInputs,
    Constraint,
    DependencyEvidence,
    InterfaceEvidence,
    LicenseEvidence,
    Requirement,
    Verdict,
)
from workers.analysis import run_analysis
from workers.ingestion import ingest_revision
from workers.recommend import (
    load_default_rules,
    recommend_for_requirement,
)
from workers.scoring import load_default_profile, score_capability_version


RULES_PATH = (
    Path(__file__).parent.parent / "config" /
    "recommendation_rules" / "default.yaml"
)


# ---------------------------------------------------------------------------
# Helpers to build in-memory candidates for pure fit tests
# ---------------------------------------------------------------------------

def _mk_candidate(
    *,
    interfaces=(),
    dependencies=(),
    licenses=(),
    intrinsic_score=0.8,
    cv_id=None,
) -> CandidateInputs:
    return CandidateInputs(
        capability_version_id=cv_id or str(uuid.uuid4()),
        intrinsic_score=intrinsic_score,
        interfaces=tuple(interfaces),
        dependencies=tuple(dependencies),
        licenses=tuple(licenses),
        pinned_revision_key="abc12345",
        pinned_source_asset_key="acme/lib",
    )


def _mk_requirement(*constraints: Constraint) -> Requirement:
    return Requirement(
        id=str(uuid.uuid4()), slug="test", description="",
        constraints=tuple(constraints),
    )


# ---------------------------------------------------------------------------
# Fit computation — required_interface
# ---------------------------------------------------------------------------

def test_fit_required_interface_matches_by_name_case_insensitive():
    req = _mk_requirement(
        Constraint(kind="required_interface", detail={"name": "Get"}),
    )
    ev_id = str(uuid.uuid4())
    cand = _mk_candidate(interfaces=[
        InterfaceEvidence(evidence_item_id=ev_id, kind="function",
                          name="get", signature="url: str"),
    ])
    result = evaluate_fit(req, cand)
    assert result.fit_score == 1.0
    assert result.blocking_gap_count == 0
    assert any(l.evidence_item_id == ev_id and l.role == "supports_match"
                for l in result.evidence_links)


def test_fit_required_interface_records_gap_when_missing():
    req = _mk_requirement(
        Constraint(kind="required_interface", detail={"name": "get"}),
    )
    cand = _mk_candidate(interfaces=[])
    result = evaluate_fit(req, cand)
    assert result.fit_score == 0.0
    assert any(g.kind == "missing_interface" for g in result.gaps)
    # Missing interface is non-blocking — it's a fit issue, not a
    # correctness/legal issue.
    assert result.blocking_gap_count == 0


def test_fit_required_interface_uses_signature_substring():
    req = _mk_requirement(
        Constraint(kind="required_interface",
                    detail={"name": "get", "signature_contains": "timeout"}),
    )
    # Wrong signature.
    cand_a = _mk_candidate(interfaces=[
        InterfaceEvidence(evidence_item_id=str(uuid.uuid4()), kind="function",
                          name="get", signature="url: str"),
    ])
    assert evaluate_fit(req, cand_a).fit_score == 0.0

    # Signature contains 'timeout' — match.
    cand_b = _mk_candidate(interfaces=[
        InterfaceEvidence(evidence_item_id=str(uuid.uuid4()), kind="function",
                          name="get", signature="url: str, timeout: int = 30"),
    ])
    assert evaluate_fit(req, cand_b).fit_score == 1.0


# ---------------------------------------------------------------------------
# Fit computation — license_allowlist
# ---------------------------------------------------------------------------

def test_fit_license_allowlist_pass_and_link_evidence():
    req = _mk_requirement(
        Constraint(kind="license_allowlist",
                    detail={"spdx_ids": ["MIT", "Apache-2.0"]}),
    )
    ev_id = str(uuid.uuid4())
    cand = _mk_candidate(licenses=[
        LicenseEvidence(evidence_item_id=ev_id, spdx_id="MIT"),
    ])
    result = evaluate_fit(req, cand)
    assert result.fit_score == 1.0
    assert result.blocking_gap_count == 0
    assert any(l.evidence_item_id == ev_id and l.role == "supports_match"
                for l in result.evidence_links)


def test_fit_license_allowlist_fails_and_gap_is_blocking():
    req = _mk_requirement(
        Constraint(kind="license_allowlist",
                    detail={"spdx_ids": ["MIT"]}),
    )
    ev_id = str(uuid.uuid4())
    cand = _mk_candidate(licenses=[
        LicenseEvidence(evidence_item_id=ev_id, spdx_id="GPL-3.0"),
    ])
    result = evaluate_fit(req, cand)
    assert result.fit_score == 0.0
    assert result.blocking_gap_count == 1
    # The observed GPL license is documented as a gap-evidence link.
    assert any(l.evidence_item_id == ev_id and l.role == "documents_gap"
                for l in result.evidence_links)


def test_fit_no_license_evidence_is_blocking():
    req = _mk_requirement(
        Constraint(kind="license_allowlist", detail={"spdx_ids": ["MIT"]}),
    )
    cand = _mk_candidate(licenses=[])
    result = evaluate_fit(req, cand)
    assert result.blocking_gap_count == 1


# ---------------------------------------------------------------------------
# Fit computation — forbidden_dependency
# ---------------------------------------------------------------------------

def test_fit_forbidden_dependency_absent_is_satisfied():
    req = _mk_requirement(
        Constraint(kind="forbidden_dependency",
                    detail={"ecosystem": "pypi", "name": "leftpad"}),
    )
    cand = _mk_candidate(dependencies=[
        DependencyEvidence(evidence_item_id=str(uuid.uuid4()),
                            ecosystem="pypi", name="httpx", kind="runtime"),
    ])
    result = evaluate_fit(req, cand)
    assert result.fit_score == 1.0
    assert result.blocking_gap_count == 0


def test_fit_forbidden_dependency_present_is_blocking():
    req = _mk_requirement(
        Constraint(kind="forbidden_dependency",
                    detail={"ecosystem": "pypi", "name": "leftpad"}),
    )
    ev_id = str(uuid.uuid4())
    cand = _mk_candidate(dependencies=[
        DependencyEvidence(evidence_item_id=ev_id,
                            ecosystem="pypi", name="leftpad", kind="runtime"),
    ])
    result = evaluate_fit(req, cand)
    assert result.blocking_gap_count == 1
    assert any(l.evidence_item_id == ev_id and l.role == "documents_gap"
                for l in result.evidence_links)


# ---------------------------------------------------------------------------
# Fit determinism
# ---------------------------------------------------------------------------

def test_fit_computed_hash_is_stable_across_calls():
    req = _mk_requirement(
        Constraint(kind="required_interface", detail={"name": "get"}),
        Constraint(kind="license_allowlist",
                    detail={"spdx_ids": ["MIT"]}),
    )
    ev_iface = str(uuid.UUID(int=1))
    ev_lic = str(uuid.UUID(int=2))
    cand = _mk_candidate(
        interfaces=[InterfaceEvidence(
            evidence_item_id=ev_iface, kind="function",
            name="get", signature="url",
        )],
        licenses=[LicenseEvidence(evidence_item_id=ev_lic, spdx_id="MIT")],
    )
    a = evaluate_fit(req, cand)
    b = evaluate_fit(req, cand)
    assert a.computed_hash == b.computed_hash


def test_fit_computed_hash_changes_when_evidence_changes():
    req = _mk_requirement(
        Constraint(kind="license_allowlist", detail={"spdx_ids": ["MIT"]}),
    )
    a = evaluate_fit(req, _mk_candidate(
        licenses=[LicenseEvidence(evidence_item_id=str(uuid.UUID(int=1)),
                                    spdx_id="MIT")]))
    b = evaluate_fit(req, _mk_candidate(
        licenses=[LicenseEvidence(evidence_item_id=str(uuid.UUID(int=1)),
                                    spdx_id="Apache-2.0")]))
    assert a.computed_hash != b.computed_hash


# ---------------------------------------------------------------------------
# Rules interpreter — loading + validation
# ---------------------------------------------------------------------------

def test_default_rules_load():
    p = load_rules(RULES_PATH)
    assert p.name == "default"
    assert p.version == 1
    assert p.default == Verdict.BUILD
    assert len(p.rules) >= 3
    assert len(p.profile_hash) == 64


def test_rules_reject_unknown_field(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndefault: BUILD\nrules:\n"
        "  - name: x\n"
        "    verdict: ADOPT\n"
        "    conditions:\n"
        "      - {field: nonsense, op: '>=', value: 0.5}\n"
    )
    with pytest.raises(RulesError, match="not in"):
        load_rules(bad)


def test_rules_reject_unknown_operator(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndefault: BUILD\nrules:\n"
        "  - name: x\n"
        "    verdict: ADOPT\n"
        "    conditions:\n"
        "      - {field: fit_score, op: '~=', value: 0.5}\n"
    )
    with pytest.raises(RulesError, match="operator"):
        load_rules(bad)


def test_rules_reject_unknown_verdict(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndefault: BUILD\nrules:\n"
        "  - name: x\n"
        "    verdict: MAYBE\n"
        "    conditions: []\n"
    )
    with pytest.raises(RulesError, match="verdict"):
        load_rules(bad)


def test_rules_reject_duplicate_names(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndefault: BUILD\nrules:\n"
        "  - {name: dup, verdict: ADOPT, conditions: []}\n"
        "  - {name: dup, verdict: ADAPT, conditions: []}\n"
    )
    with pytest.raises(RulesError, match="duplicate"):
        load_rules(bad)


# ---------------------------------------------------------------------------
# Rules interpreter — evaluation
# ---------------------------------------------------------------------------

def test_rules_first_match_wins():
    p = load_rules(RULES_PATH)
    # Perfect scores → strong_adopt (first rule).
    d = evaluate(p, fit_score=1.0, intrinsic_score=1.0, blocking_gap_count=0)
    assert d.verdict == Verdict.ADOPT
    assert d.rule_name == "strong_adopt"


def test_rules_falls_through_to_default_when_no_match():
    p = load_rules(RULES_PATH)
    # High fit, but blocking gaps: doesn't hit adopt (needs blocking==0)
    # or adapt, does hit reference (fit>=0.3, intrinsic>=0.5).
    # Now craft one where NOTHING matches: fit_score below reject
    # threshold, but reject fires on fit<0.3, so it will match reject.
    # Do this instead: fit_score=0.4, intrinsic=0.4 — fit>=0.3 but
    # intrinsic<0.5, so reference misses; reject requires fit<0.3, so
    # reject misses too. Nothing matches → default (BUILD).
    d = evaluate(p, fit_score=0.4, intrinsic_score=0.4, blocking_gap_count=0)
    assert d.verdict == Verdict.BUILD
    assert d.rule_name == "__default__"


def test_rules_reject_fires_below_threshold():
    p = load_rules(RULES_PATH)
    d = evaluate(p, fit_score=0.1, intrinsic_score=0.9, blocking_gap_count=0)
    assert d.verdict == Verdict.REJECT


def test_rules_adapt_fires_on_good_but_not_great_fit():
    p = load_rules(RULES_PATH)
    d = evaluate(p, fit_score=0.7, intrinsic_score=0.7, blocking_gap_count=0)
    assert d.verdict == Verdict.ADAPT


# ---------------------------------------------------------------------------
# End-to-end fixture: real capability_version with real evidence
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


def _bootstrap_cv(conn, registry, external_key, revision_key, files):
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


def _create_requirement(conn, project_name, slug, description, *constraints):
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
            import json as _json
            cur.execute(
                "INSERT INTO requirement_constraint "
                "(project_requirement_id, kind, detail) "
                "VALUES (%s, %s, %s::jsonb)",
                (rid, kind, _json.dumps(detail)),
            )
    conn.commit()
    return rid


_STRONG_FILES = {
    "pyproject.toml": (
        b"[project]\nname = 'good-http'\nversion = '1.0.0'\n"
        b"dependencies = ['httpx']\n"
    ),
    "src/good_http/__init__.py": b"",
    "src/good_http/api.py": (
        b"def get(url: str, timeout: int = 30) -> str:\n"
        b"    return url\n"
        b"def post(url: str, body: dict) -> str:\n"
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
# THE STEP 12 DONE-WHEN
# ---------------------------------------------------------------------------

def test_recommendation_names_the_exact_revision_to_pin(conn, registry):
    """
    THE done-when, first half. A verdict of ADOPT/ADAPT/REFERENCE MUST
    carry chosen_capability_version_id AND pinned_revision_key AND
    pinned_source_asset_key. The schema enforces this via the
    recommendation_pin_required CHECK.
    """
    cv_id = _bootstrap_cv(conn, registry, "acme/good-http",
                            "sha_1234567890abcdef", _STRONG_FILES)
    req_id = _create_requirement(
        conn, "demo", "http-client", "an HTTP client",
        ("license_allowlist", {"spdx_ids": ["MIT", "Apache-2.0"]}),
        ("required_interface", {"name": "get"}),
    )
    outcome = recommend_for_requirement(
        conn,
        project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    # The verdict should be a positive one (fit_score high, intrinsic
    # score >= 0.6 from the fully-populated repo).
    assert outcome.verdict in (Verdict.ADOPT, Verdict.ADAPT, Verdict.REFERENCE)

    # Pinned to the exact revision.
    assert str(outcome.chosen_capability_version_id) == str(cv_id)
    assert outcome.pinned_revision_key == "sha_1234567890abcdef"
    assert outcome.pinned_source_asset_key == "acme/good-http"

    # DB row carries the same values.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chosen_capability_version_id, pinned_revision_key, "
            "pinned_source_asset_key FROM recommendation WHERE id = %s",
            (str(outcome.recommendation_id),),
        )
        row = cur.fetchone()
    assert str(row[0]) == str(cv_id)
    assert row[1] == "sha_1234567890abcdef"
    assert row[2] == "acme/good-http"


def test_every_material_claim_links_to_a_real_evidence_row(conn, registry):
    """
    THE done-when, second half. Walk from recommendation → candidate →
    fit_evaluation → fit_evidence_link and verify EVERY link targets a
    real evidence_item row for this revision.
    """
    cv_id = _bootstrap_cv(conn, registry, "acme/good-http",
                            "sha_2", _STRONG_FILES)
    req_id = _create_requirement(
        conn, "demo", "http-client", "an HTTP client",
        ("license_allowlist", {"spdx_ids": ["MIT"]}),
        ("required_interface", {"name": "get"}),
    )
    outcome = recommend_for_requirement(
        conn,
        project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fel.id, fel.evidence_item_id, fel.role
            FROM recommendation_candidate rc
            JOIN fit_evaluation fe ON fe.id = rc.fit_evaluation_id
            JOIN fit_evidence_link fel ON fel.fit_evaluation_id = fe.id
            LEFT JOIN evidence_item ei ON ei.id = fel.evidence_item_id
            WHERE rc.recommendation_id = %s
              AND ei.id IS NULL
            """,
            (str(outcome.recommendation_id),),
        )
        orphans = cur.fetchall()
    assert orphans == [], (
        f"fit_evidence_link rows whose evidence_item_id doesn't resolve "
        f"to a real evidence_item: {orphans}"
    )

    # And there should be at least one link, or the claim is empty.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM recommendation_candidate rc
            JOIN fit_evaluation fe ON fe.id = rc.fit_evaluation_id
            JOIN fit_evidence_link fel ON fel.fit_evaluation_id = fe.id
            WHERE rc.recommendation_id = %s
            """,
            (str(outcome.recommendation_id),),
        )
        assert cur.fetchone()[0] >= 2, (
            "expected at least a license link and an interface link"
        )


# ---------------------------------------------------------------------------
# Schema-enforced "must pin"
# ---------------------------------------------------------------------------

def test_check_constraint_blocks_unpinned_adopt(conn):
    """
    Belt-and-braces: the recommendation_pin_required CHECK constraint
    refuses an ADOPT row without chosen_capability_version_id + pinned
    fields, even via raw SQL that bypasses the orchestrator.
    """
    with conn.cursor() as cur:
        # Fixture project/requirement/rules_profile so FKs resolve.
        cur.execute(
            "INSERT INTO project (name) VALUES ('x') RETURNING id"
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO project_requirement "
            "(project_id, slug, description) VALUES (%s, 's', 'd') "
            "RETURNING id", (pid,),
        )
        rid = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO recommendation_rules_profile
              (name, version, profile_hash, rules)
            VALUES ('empty', 1, 'hash', '[]'::jsonb) RETURNING id
            """
        )
        prof_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recommendation
                  (project_requirement_id, rules_profile_id, verdict,
                   rule_name, reason)
                VALUES (%s, %s, 'ADOPT', 'test', 'test')
                """,
                (str(rid), str(prof_id)),
            )


def test_check_constraint_allows_unpinned_reject(conn):
    """REJECT and BUILD are legal without a pinned revision — nothing
    to pin."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO project (name) VALUES ('x') RETURNING id")
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
            "VALUES ('empty', 1, 'hash', '[]'::jsonb) RETURNING id"
        )
        prof_id = cur.fetchone()[0]
        # No pin fields → should succeed.
        cur.execute(
            """
            INSERT INTO recommendation
              (project_requirement_id, rules_profile_id, verdict,
               rule_name, reason)
            VALUES (%s, %s, 'REJECT', 'test', 'no candidate fits')
            """,
            (str(rid), str(prof_id)),
        )
    conn.commit()   # no exception


# ---------------------------------------------------------------------------
# Orchestrator behaviour — ranking, BUILD default, fit + intrinsic separation
# ---------------------------------------------------------------------------

def test_no_candidates_yields_build_verdict(conn):
    """No capability_version has a scorecard → BUILD."""
    req_id = _create_requirement(
        conn, "empty-proj", "http-client", "an HTTP client",
        ("required_interface", {"name": "get"}),
    )
    outcome = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    assert outcome.verdict == Verdict.BUILD
    assert outcome.chosen_capability_version_id is None
    assert outcome.pinned_revision_key is None
    assert outcome.rule_name == "__no_candidates__"
    assert outcome.candidate_count == 0


def test_orchestrator_ranks_candidates_and_persists_them(conn, registry):
    """Two candidates: one strong fit, one weak. The strong one is
    rank=1 and matches chosen_capability_version_id."""
    strong = _bootstrap_cv(
        conn, registry, "acme/good", "sha_strong", _STRONG_FILES
    )
    # Weak candidate: same MIT license but no matching interface.
    weak_files = {
        **_STRONG_FILES,
        "pyproject.toml": (
            b"[project]\nname = 'weak-http'\nversion = '0.1.0'\n"
        ),
        "src/good_http/api.py": (
            b"def something_else():\n    return 1\n"
        ),
    }
    weak = _bootstrap_cv(
        conn, registry, "acme/weak", "sha_weak", weak_files
    )

    req_id = _create_requirement(
        conn, "demo", "http-client", "an HTTP client",
        ("license_allowlist", {"spdx_ids": ["MIT"]}),
        ("required_interface", {"name": "get"}),
    )
    outcome = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    # Strong should win.
    assert str(outcome.chosen_capability_version_id) == str(strong)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability_version_id, rank FROM recommendation_candidate "
            "WHERE recommendation_id = %s ORDER BY rank",
            (str(outcome.recommendation_id),),
        )
        rows = cur.fetchall()
    assert rows[0][1] == 1
    assert str(rows[0][0]) == str(strong)
    assert rows[1][1] == 2
    assert str(rows[1][0]) == str(weak)


def test_fit_and_intrinsic_scores_live_in_separate_tables(conn, registry):
    """
    The non-negotiable: fit score and intrinsic score are stored on
    fit_evaluation and scorecard respectively — never merged.
    """
    _bootstrap_cv(
        conn, registry, "acme/good", "sha_9", _STRONG_FILES
    )
    req_id = _create_requirement(
        conn, "demo", "http-client", "an HTTP client",
        ("license_allowlist", {"spdx_ids": ["MIT"]}),
    )
    recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    with conn.cursor() as cur:
        # fit_score column exists on fit_evaluation, NOT on scorecard.
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'scorecard'"
        )
        scorecard_cols = {r[0] for r in cur.fetchall()}
        assert "fit_score" not in scorecard_cols

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'fit_evaluation'"
        )
        fit_cols = {r[0] for r in cur.fetchall()}
        assert "fit_score" in fit_cols
        assert "intrinsic_score" not in fit_cols, (
            "fit_evaluation must not carry intrinsic_score — they never merge"
        )


def test_reject_verdict_when_no_candidate_meets_threshold(conn, registry):
    """
    Only weak candidates exist. fit_score below 0.3 → REJECT.
    """
    weak_files = {
        **_STRONG_FILES,
        "pyproject.toml": (
            b"[project]\nname = 'weak'\nversion = '0.1.0'\n"
        ),
        "src/good_http/api.py": b"def unrelated():\n    return 1\n",
    }
    _bootstrap_cv(conn, registry, "acme/weak", "sha", weak_files)

    req_id = _create_requirement(
        conn, "demo", "http-client", "an HTTP client",
        ("required_interface", {"name": "get"}),
        ("required_interface", {"name": "post"}),
        ("required_interface", {"name": "put"}),
        # No license constraint so it doesn't blocking-gap.
    )
    outcome = recommend_for_requirement(
        conn, project_requirement_id=req_id,
        rules_profile=load_default_rules(),
    )
    # 0 out of 3 interfaces matched → fit_score = 0 → REJECT.
    assert outcome.verdict == Verdict.REJECT
    assert outcome.chosen_capability_version_id is None
    assert outcome.pinned_revision_key is None
