"""
Step 10 done-when:
  "A repository with an incompatible license cannot reach CATALOGED by
   any code path."

The enforcement is in workers.gates.can_publish(). Step 11's lifecycle
transitions call it before promoting to CATALOGED. Here we prove:
  1. can_publish() refuses when a blocking gate has failed
  2. can_publish() refuses when gates haven't been evaluated at all
  3. can_publish() refuses when a registered gate wasn't evaluated
  4. can_publish() accepts only when either (a) no blocking failures,
     or (b) every blocking failure has a risk_acceptance
  5. Silent bypass is impossible — risk_acceptance requires a non-empty
     reason (enforced at TWO layers: CHECK constraint + Python)
  6. risk_acceptance is append-only (rules block UPDATE/DELETE)
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.registry import promote_revision
from core.policy.gates import (
    ACCEPTABLE_LICENSES,
    CriticalVulnerabilityGate,
    GateContext,
    GateEvidence,
    GateStatus,
    LicenseCompatibilityGate,
    MissingProvenanceGate,
    UnsafeDeclaredPermissionsGate,
    default_gates,
)
from workers.analysis import run_analysis
from workers.gates import (
    accept_risk,
    can_publish,
    evaluate_gates,
)
from workers.ingestion import ingest_revision


# ---------------------------------------------------------------------------
# Pure gate unit tests — evaluate without touching the DB
# ---------------------------------------------------------------------------

def _ctx(has_binding=True, content_hash="abc123"):
    return GateContext(has_source_binding=has_binding, content_hash=content_hash)


# LicenseCompatibilityGate --------------------------------------------------

def test_license_gate_passes_on_MIT():
    g = LicenseCompatibilityGate()
    result = g.evaluate(
        [GateEvidence(id="1", evidence_type="license",
                      extracted_value={"spdx_id": "MIT"})],
        _ctx(),
    )
    assert result.status == GateStatus.PASS


def test_license_gate_fails_on_GPL_3_0():
    """The most common way this test would fire in production: someone
    tries to catalog a copyleft library into a permissive-licensed
    context."""
    g = LicenseCompatibilityGate()
    result = g.evaluate(
        [GateEvidence(id="1", evidence_type="license",
                      extracted_value={"spdx_id": "GPL-3.0"})],
        _ctx(),
    )
    assert result.status == GateStatus.FAIL
    assert "no acceptable license" in result.reason


def test_license_gate_fails_on_unknown_spdx():
    g = LicenseCompatibilityGate()
    result = g.evaluate(
        [GateEvidence(id="1", evidence_type="license",
                      extracted_value={"spdx_id": "unknown"})],
        _ctx(),
    )
    assert result.status == GateStatus.FAIL
    assert "unknown" in result.reason


def test_license_gate_fails_when_no_license_evidence():
    g = LicenseCompatibilityGate()
    result = g.evaluate([], _ctx())
    assert result.status == GateStatus.FAIL
    assert "no license" in result.reason.lower()


def test_license_gate_passes_when_any_license_is_acceptable():
    """If a repo has both LICENSE (Apache-2.0) and NOTICE (unknown),
    the Apache-2.0 is enough."""
    g = LicenseCompatibilityGate()
    result = g.evaluate(
        [
            GateEvidence(id="1", evidence_type="license",
                         extracted_value={"spdx_id": "Apache-2.0"}),
            GateEvidence(id="2", evidence_type="license",
                         extracted_value={"spdx_id": "unknown"}),
        ],
        _ctx(),
    )
    assert result.status == GateStatus.PASS


def test_license_allowlist_excludes_gpl_and_lgpl():
    """Regression: adding GPL to the allowlist is a policy call that
    must be conscious. Assert what the allowlist contains today."""
    for restricted in ("GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0"):
        assert restricted not in ACCEPTABLE_LICENSES
    for permissive in ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"):
        assert permissive in ACCEPTABLE_LICENSES


# CriticalVulnerabilityGate -------------------------------------------------

def test_critical_vuln_gate_passes_when_no_secret_indicators():
    g = CriticalVulnerabilityGate()
    assert g.evaluate([], _ctx()).status == GateStatus.PASS


def test_critical_vuln_gate_fails_on_any_secret_indicator():
    g = CriticalVulnerabilityGate()
    result = g.evaluate(
        [GateEvidence(id="1", evidence_type="secret_indicator",
                      extracted_value={"pattern_name": "aws_access_key"})],
        _ctx(),
    )
    assert result.status == GateStatus.FAIL
    assert result.detail["count"] == 1


# MissingProvenanceGate -----------------------------------------------------

def test_missing_provenance_gate_passes_with_binding_and_hash():
    g = MissingProvenanceGate()
    assert g.evaluate([], _ctx(has_binding=True, content_hash="h")).status == \
        GateStatus.PASS


def test_missing_provenance_gate_fails_without_binding():
    g = MissingProvenanceGate()
    result = g.evaluate([], _ctx(has_binding=False, content_hash=None))
    assert result.status == GateStatus.FAIL
    assert "source_binding" in result.reason


def test_missing_provenance_gate_fails_without_content_hash():
    g = MissingProvenanceGate()
    result = g.evaluate([], _ctx(has_binding=True, content_hash=None))
    assert result.status == GateStatus.FAIL
    assert "content_hash" in result.reason


# UnsafeDeclaredPermissionsGate --------------------------------------------

def test_unsafe_permissions_gate_warns_on_setup_py():
    """WARN, not FAIL — advisory. Reviewer sees it and decides."""
    g = UnsafeDeclaredPermissionsGate()
    result = g.evaluate(
        [GateEvidence(id="1", evidence_type="setup_py_detected",
                      extracted_value={})],
        _ctx(),
    )
    assert result.status == GateStatus.WARN
    assert g.is_blocking is False


def test_unsafe_permissions_gate_passes_without_setup_py():
    g = UnsafeDeclaredPermissionsGate()
    assert g.evaluate([], _ctx()).status == GateStatus.PASS


# ---------------------------------------------------------------------------
# End-to-end fixture: a promoted capability_version to run gates against
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


def _promote(conn, registry, external_key, revision_key, files):
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
    return out.capability_version_id


_CLEAN_FILES = {
    "pyproject.toml": (
        b"[project]\nname = 'clean-lib'\nversion = '0.1.0'\n"
        b"dependencies = ['httpx']\n"
    ),
    "src/x.py": b"def hi(): return 1\n",
    "tests/test_x.py": b"def test_hi(): pass\n",
    "LICENSE": (
        b"MIT License\n\n"
        b"Permission is hereby granted, free of charge, to any "
        b"person obtaining a copy\n"
    ),
}


# GPL-3.0 header — LicenseExtractor's high-confidence match phrase.
_GPL_LICENSE_TEXT = (
    b"                    GNU GENERAL PUBLIC LICENSE\n"
    b"                       Version 3, 29 June 2007\n\n"
    b"Copyright (C) 2007 Free Software Foundation, Inc.\n"
)


# ---------------------------------------------------------------------------
# Orchestrator — evaluate_gates persists correctly
# ---------------------------------------------------------------------------

def test_evaluate_gates_writes_one_row_per_gate(conn, registry):
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    outcome = evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    assert len(outcome.gates) == 4    # four gates in default_gates()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM gate_result "
            "WHERE capability_version_id = %s",
            (str(cv_id),),
        )
        assert cur.fetchone()[0] == 4


def test_evaluate_gates_upserts_on_re_evaluation(conn, registry):
    """Re-evaluating produces the same row count, updated in place."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM gate_result "
            "WHERE capability_version_id = %s",
            (str(cv_id),),
        )
        assert cur.fetchone()[0] == 4     # not 8


def test_evaluate_gates_records_blocking_flag_per_gate(conn, registry):
    """LicenseCompatibilityGate is blocking; UnsafeDeclaredPermissionsGate
    is not. gate_result.is_blocking must reflect that."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gate_name, is_blocking FROM gate_result "
            "WHERE capability_version_id = %s ORDER BY gate_name",
            (str(cv_id),),
        )
        by_name = dict(cur.fetchall())
    assert by_name["license_compatibility"] is True
    assert by_name["critical_vulnerability"] is True
    assert by_name["missing_provenance"] is True
    assert by_name["unsafe_declared_permissions"] is False


# ---------------------------------------------------------------------------
# THE STEP 10 DONE-WHEN
# ---------------------------------------------------------------------------

def test_incompatible_license_cannot_be_published_by_any_code_path(
    conn, registry
):
    """
    THE done-when: a repo with GPL-3.0 cannot reach CATALOGED. We
    prove it at the publication-guard layer: can_publish() refuses.
    Step 11's lifecycle transitions will call can_publish() before
    promoting to CATALOGED — so 'no code path' is enforced by making
    the guard the single choke point.
    """
    gpl_files = {**_CLEAN_FILES, "LICENSE": _GPL_LICENSE_TEXT}
    cv_id = _promote(conn, registry, "acme/gpl-lib", "sha1", gpl_files)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()

    decision = can_publish(conn, capability_version_id=cv_id)
    assert decision.allowed is False, (
        "capability_version with GPL-3.0 license MUST NOT be publishable"
    )
    names = [f.gate_name for f in decision.blocking_failures]
    assert "license_compatibility" in names


def test_can_publish_refuses_when_no_gates_evaluated(conn, registry):
    """A capability_version with zero gate_result rows is un-vetted. The
    guard refuses — publishing an un-evaluated thing beats every purpose
    the gates serve."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    decision = can_publish(conn, capability_version_id=cv_id)
    assert decision.allowed is False
    assert any("no gates" in w for w in decision.warnings)


def test_can_publish_refuses_when_registered_gate_missing(conn, registry):
    """Someone runs a subset of gates and skips license — the guard
    catches it via the 'registered gate never evaluated' check."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    # Only run the non-license gates.
    subset = [
        CriticalVulnerabilityGate(),
        MissingProvenanceGate(),
        UnsafeDeclaredPermissionsGate(),
    ]
    evaluate_gates(conn, capability_version_id=cv_id, gates=subset)
    conn.commit()

    decision = can_publish(conn, capability_version_id=cv_id)
    assert decision.allowed is False
    missing = [f.gate_name for f in decision.blocking_failures]
    assert "license_compatibility" in missing


def test_can_publish_allows_when_all_blocking_gates_pass(conn, registry):
    """The clean-fixture happy path — a repo with MIT + no secrets +
    proper provenance publishes."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    decision = can_publish(conn, capability_version_id=cv_id)
    assert decision.allowed is True
    assert decision.blocking_failures == []


def test_warn_status_does_not_block_publication(conn, registry):
    """setup.py present → WARN on unsafe_declared_permissions, but MIT
    license and no secrets so nothing blocks."""
    files_with_setup_py = {
        **_CLEAN_FILES,
        "setup.py": b"from setuptools import setup\nsetup(name='x')\n",
    }
    cv_id = _promote(
        conn, registry, "acme/setup", "sha1", files_with_setup_py
    )
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    decision = can_publish(conn, capability_version_id=cv_id)
    assert decision.allowed is True
    assert any("unsafe_declared_permissions" in w for w in decision.warnings)


# ---------------------------------------------------------------------------
# Risk acceptance — the ONLY way to override
# ---------------------------------------------------------------------------

def test_risk_acceptance_unblocks_a_specific_gate(conn, registry):
    """
    A GPL-3.0 repo is normally unpublishable. A risk_acceptance row
    with a non-empty reason permits publication of THAT SPECIFIC
    capability_version — and only that one.
    """
    gpl_files = {**_CLEAN_FILES, "LICENSE": _GPL_LICENSE_TEXT}
    cv_id = _promote(conn, registry, "acme/gpl-lib", "sha1", gpl_files)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()

    decision_before = can_publish(conn, capability_version_id=cv_id)
    assert decision_before.allowed is False
    # Find the specific gate_result to accept.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result WHERE capability_version_id = %s "
            "AND gate_name = 'license_compatibility'",
            (str(cv_id),),
        )
        gate_id = cur.fetchone()[0]

    accept_risk(
        conn,
        gate_result_id=gate_id,
        accepted_by="alice",
        reason="internal-use only, GPL propagation reviewed with legal",
    )
    conn.commit()

    decision_after = can_publish(conn, capability_version_id=cv_id)
    assert decision_after.allowed is True


def test_risk_acceptance_requires_non_empty_reason_at_python_layer(conn, registry):
    """Empty and whitespace-only reasons are refused before hitting SQL."""
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result "
            "WHERE capability_version_id = %s LIMIT 1",
            (str(cv_id),),
        )
        gate_id = cur.fetchone()[0]

    for empty in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="reason"):
            accept_risk(
                conn, gate_result_id=gate_id,
                accepted_by="alice", reason=empty,
            )


def test_risk_acceptance_check_constraint_blocks_empty_reason_at_db_layer(
    conn, registry
):
    """
    Belt-and-braces: even if someone bypasses accept_risk() and writes
    raw SQL, the CHECK constraint rejects empty reasons.
    """
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result "
            "WHERE capability_version_id = %s LIMIT 1",
            (str(cv_id),),
        )
        gate_id = cur.fetchone()[0]

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO risk_acceptance "
                "(gate_result_id, accepted_by, reason) VALUES (%s, %s, %s)",
                (str(gate_id), "alice", "   "),   # whitespace-only
            )


def test_risk_acceptance_is_append_only(conn, registry):
    """
    Rules block UPDATE and DELETE — matches state_transition and
    evidence_item. The audit trail is real, not aspirational.
    """
    cv_id = _promote(conn, registry, "acme/clean", "sha1", _CLEAN_FILES)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result "
            "WHERE capability_version_id = %s LIMIT 1",
            (str(cv_id),),
        )
        gate_id = cur.fetchone()[0]

    ra_id = accept_risk(
        conn, gate_result_id=gate_id,
        accepted_by="alice", reason="test",
    )
    conn.commit()

    # UPDATE and DELETE are silent no-ops (rules).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE risk_acceptance SET reason = 'edited' WHERE id = %s",
            (str(ra_id),),
        )
        cur.execute("DELETE FROM risk_acceptance WHERE id = %s", (str(ra_id),))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT reason FROM risk_acceptance WHERE id = %s", (str(ra_id),)
        )
        row = cur.fetchone()
    assert row is not None, "DELETE should have been silently ignored"
    assert row[0] == "test", "UPDATE should have been silently ignored"


def test_risk_acceptance_is_gate_specific_not_wildcard(conn, registry):
    """
    Accepting risk on gate X doesn't unblock gate Y. A cap_version
    that fails TWO blocking gates needs TWO risk_acceptance rows to
    publish.
    """
    # Contrive a repo that fails both license AND critical_vulnerability.
    bad_files = {
        **_CLEAN_FILES,
        "LICENSE": _GPL_LICENSE_TEXT,
        "docs/env.md": (
            b"Example: AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        ),
    }
    cv_id = _promote(conn, registry, "acme/bad", "sha1", bad_files)
    evaluate_gates(conn, capability_version_id=cv_id)
    conn.commit()

    initial = can_publish(conn, capability_version_id=cv_id)
    failing_names = {f.gate_name for f in initial.blocking_failures}
    assert failing_names >= {"license_compatibility", "critical_vulnerability"}

    # Accept only the license risk.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result WHERE capability_version_id = %s "
            "AND gate_name = 'license_compatibility'",
            (str(cv_id),),
        )
        license_gate_id = cur.fetchone()[0]
    accept_risk(
        conn, gate_result_id=license_gate_id,
        accepted_by="alice", reason="reviewed",
    )
    conn.commit()

    partial = can_publish(conn, capability_version_id=cv_id)
    assert partial.allowed is False, (
        "risk_acceptance is per-gate; accepting license doesn't unblock "
        "critical_vulnerability"
    )
    remaining = {f.gate_name for f in partial.blocking_failures}
    assert "critical_vulnerability" in remaining
    assert "license_compatibility" not in remaining
