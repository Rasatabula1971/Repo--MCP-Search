"""
Step 11 done-when:
  "Each guard can be individually failed in a test, and the capability
   stays unpublished for the correctly stated reason."

Seven guards. Seven failing tests. Plus:
  - Happy path: all guards pass → advance_to(CATALOGED) succeeds
  - Illegal transitions (candidate → cataloged) raise
  - Transitions leave state_transition rows + outbox events
  - Advancing an already-cataloged version is a no-op
"""
from __future__ import annotations

import uuid

import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.lifecycle import (
    CONFIDENCE_THRESHOLD,
    AdvanceOutcome,
    GuardFailure,
    GuardsFailed,
    IllegalTransition,
    LifecycleState,
    advance_to,
    check_publication_guards,
    current_state,
)
from core.capability.registry import promote_revision
from workers.analysis import run_analysis
from workers.gates import accept_risk, evaluate_gates
from workers.ingestion import ingest_revision
from workers.scoring import load_default_profile, score_capability_version


# ---------------------------------------------------------------------------
# Fixture: a capability_version at each preparation level
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


_HAPPY_FILES = {
    "pyproject.toml": (
        b"[project]\nname = 'clean-lib'\nversion = '0.1.0'\n"
        b"dependencies = ['httpx']\n"
    ),
    "src/x.py": (
        b"def public_fn(a):\n    return a\n"
        b"class PublicClass:\n    def method(self): pass\n"
    ),
    "tests/test_x.py": b"def test_a(): pass\n",
    "LICENSE": (
        b"MIT License\n\n"
        b"Permission is hereby granted, free of charge, to any "
        b"person obtaining a copy\n"
    ),
}


def _bootstrap_revision(conn, registry, external_key, revision_key, files):
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
    return rid


def _prepare_fully(conn, registry, external_key="acme/clean",
                    revision_key="sha1", files=None):
    """
    Bring a capability_version all the way to "ready to publish":
    ingested, analysed, promoted, scored, gates evaluated, summary set.
    Individual tests break specific guards from this baseline.
    """
    if files is None:
        files = _HAPPY_FILES
    rid = _bootstrap_revision(conn, registry, external_key, revision_key, files)
    ingest_revision(conn, registry=registry, source_revision_id=rid)
    run_analysis(conn, registry=registry, source_revision_id=rid)
    out = promote_revision(
        conn, registry=registry, source_revision_id=rid
    )
    cv_id = out.capability_version_id

    # Score.
    score_capability_version(
        conn,
        capability_version_id=cv_id,
        profile=load_default_profile(),
    )
    # Evaluate gates.
    evaluate_gates(conn, capability_version_id=cv_id)
    # Populate summary.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability_version SET summary = %s WHERE id = %s",
            ("An MIT-licensed test capability.", str(cv_id)),
        )
    conn.commit()
    return cv_id


# ---------------------------------------------------------------------------
# Helpers to fail one guard at a time
# ---------------------------------------------------------------------------

def _guard_names(failures: list[GuardFailure]) -> list[str]:
    return sorted(f.guard_name for f in failures)


def _reason_for(failures: list[GuardFailure], name: str) -> str:
    for f in failures:
        if f.guard_name == name:
            return f.reason
    raise AssertionError(f"no failure for guard {name!r}")


# ---------------------------------------------------------------------------
# THE STEP 11 DONE-WHEN — one test per guard, breaking it individually
# ---------------------------------------------------------------------------

# Guard 1: current_revision -------------------------------------------------

def test_guard_current_revision_fails_when_newer_snapshotted_exists(
    conn, registry
):
    """Baseline is ready to publish. Then we ingest+snapshot a newer
    revision of the SAME asset. The original version's current_revision
    guard must fail."""
    cv_id = _prepare_fully(conn, registry)

    # Ingest a newer revision on the same asset.
    fake = registry.get("fake")
    fake.add_revision("acme/clean", "sha2", {**_HAPPY_FILES,
                       "src/x.py": b"def public_fn(a): return a + 1\n"})
    # Reuse the existing asset id — look it up.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.source_asset_id
            FROM capability_source_binding csb
            JOIN source_revision r ON r.id = csb.source_revision_id
            WHERE csb.capability_version_id = %s
            LIMIT 1
            """,
            (str(cv_id),),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'sha2') RETURNING id",
            (asset_id,),
        )
        newer_rid = cur.fetchone()[0]
    conn.commit()
    ingest_revision(conn, registry=registry, source_revision_id=newer_rid)

    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "current_revision" in _guard_names(failures)
    assert "newer snapshotted revision" in _reason_for(
        failures, "current_revision"
    )


# Guard 2: required_scorecard -----------------------------------------------

def test_guard_required_scorecard_fails_when_no_scorecard(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    # Delete the scorecard.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scorecard WHERE capability_version_id = %s",
                    (str(cv_id),))
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "required_scorecard" in _guard_names(failures)
    assert "no scorecard" in _reason_for(failures, "required_scorecard")


# Guard 3: confidence_threshold --------------------------------------------

def test_guard_confidence_threshold_fails_when_below_threshold(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    # Lower the scorecard's confidence below the threshold.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scorecard SET confidence = %s "
            "WHERE capability_version_id = %s",
            (max(0.0, CONFIDENCE_THRESHOLD - 0.1), str(cv_id)),
        )
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "confidence_threshold" in _guard_names(failures)
    reason = _reason_for(failures, "confidence_threshold")
    assert "below threshold" in reason


# Guard 4: no_blocking_gate ------------------------------------------------

def test_guard_no_blocking_gate_fails_when_license_incompatible(
    conn, registry
):
    """Promote a GPL repo, do everything else right — the no_blocking_gate
    guard fails because the license gate blocks."""
    gpl_files = {**_HAPPY_FILES, "LICENSE": (
        b"                    GNU GENERAL PUBLIC LICENSE\n"
        b"                       Version 3, 29 June 2007\n\n"
        b"Copyright (C) 2007 Free Software Foundation, Inc.\n"
    )}
    cv_id = _prepare_fully(
        conn, registry, external_key="acme/gpl", revision_key="sha1",
        files=gpl_files,
    )
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "no_blocking_gate" in _guard_names(failures)
    assert "license_compatibility" in _reason_for(failures, "no_blocking_gate")


# Guard 5: source_binding --------------------------------------------------

def test_guard_source_binding_fails_when_no_bindings(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM capability_source_binding "
            "WHERE capability_version_id = %s",
            (str(cv_id),),
        )
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "source_binding" in _guard_names(failures)


# Guard 6: summary ---------------------------------------------------------

def test_guard_summary_fails_when_summary_empty(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability_version SET summary = NULL WHERE id = %s",
            (str(cv_id),),
        )
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "summary" in _guard_names(failures)
    assert "summary is empty" in _reason_for(failures, "summary")


def test_guard_summary_fails_when_summary_whitespace_only(conn, registry):
    """Whitespace-only summary is empty, per the guard's trim check."""
    cv_id = _prepare_fully(conn, registry)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability_version SET summary = %s WHERE id = %s",
            ("   \n\t  ", str(cv_id)),
        )
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "summary" in _guard_names(failures)


# Guard 7: interfaces ------------------------------------------------------

def test_guard_interfaces_fails_when_no_interface_rows(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM capability_interface WHERE capability_version_id = %s",
            (str(cv_id),),
        )
    conn.commit()
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert "interfaces" in _guard_names(failures)


# ---------------------------------------------------------------------------
# Happy path: all guards pass → advance to cataloged succeeds
# ---------------------------------------------------------------------------

def test_happy_path_all_seven_guards_pass(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    failures = check_publication_guards(
        conn, capability_version_id=cv_id
    )
    assert failures == [], (
        f"expected no failures, got {[(f.guard_name, f.reason) for f in failures]}"
    )


def test_full_advance_pipeline_reaches_cataloged(conn, registry):
    """Baseline → analyzed → verified → cataloged, all via advance_to."""
    cv_id = _prepare_fully(conn, registry)
    assert current_state(conn, cv_id) == LifecycleState.CANDIDATE

    o1 = advance_to(conn, capability_version_id=cv_id,
                     target=LifecycleState.ANALYZED)
    conn.commit()
    assert o1.previous_state == "candidate"
    assert o1.new_state == "analyzed"
    assert current_state(conn, cv_id) == LifecycleState.ANALYZED

    o2 = advance_to(conn, capability_version_id=cv_id,
                     target=LifecycleState.VERIFIED)
    conn.commit()
    assert o2.new_state == "verified"

    o3 = advance_to(conn, capability_version_id=cv_id,
                     target=LifecycleState.CATALOGED)
    conn.commit()
    assert o3.new_state == "cataloged"
    assert current_state(conn, cv_id) == LifecycleState.CATALOGED


def test_advance_leaves_state_transition_rows_and_outbox_events(
    conn, registry
):
    cv_id = _prepare_fully(conn, registry)
    for target in (LifecycleState.ANALYZED, LifecycleState.VERIFIED,
                    LifecycleState.CATALOGED):
        advance_to(conn, capability_version_id=cv_id, target=target)
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_state FROM state_transition "
            "WHERE entity_kind = 'capability_version' AND entity_id = %s "
            "ORDER BY occurred_at",
            (str(cv_id),),
        )
        states = [r[0] for r in cur.fetchall()]
    assert states == ["analyzed", "verified", "cataloged"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_type FROM outbox_event "
            "WHERE aggregate_id = %s ORDER BY id",
            (str(cv_id),),
        )
        events = [r[0] for r in cur.fetchall()]
    assert events == [
        "capability_version.analyzed",
        "capability_version.verified",
        "capability_version.cataloged",
    ]


# ---------------------------------------------------------------------------
# Guards fire ONLY on the transition to CATALOGED
# ---------------------------------------------------------------------------

def test_advance_to_cataloged_raises_when_guards_fail(conn, registry):
    """A version at VERIFIED that then loses its scorecard cannot
    advance to cataloged — the guard fires and raises GuardsFailed."""
    cv_id = _prepare_fully(conn, registry)
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.ANALYZED)
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.VERIFIED)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM scorecard WHERE capability_version_id = %s",
                    (str(cv_id),))
    conn.commit()

    with pytest.raises(GuardsFailed) as exc_info:
        advance_to(conn, capability_version_id=cv_id,
                    target=LifecycleState.CATALOGED)
    assert "required_scorecard" in {f.guard_name for f in exc_info.value.failures}


def test_advance_to_analyzed_and_verified_does_not_check_publication_guards(
    conn, registry
):
    """Guards only fire on the transition to CATALOGED. A capability
    version can reach VERIFIED without a summary — it just can't reach
    CATALOGED without one."""
    cv_id = _prepare_fully(conn, registry)
    # Clear summary — should not block ANALYZED or VERIFIED.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability_version SET summary = NULL WHERE id = %s",
            (str(cv_id),),
        )
    conn.commit()
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.ANALYZED)
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.VERIFIED)
    conn.commit()
    assert current_state(conn, cv_id) == LifecycleState.VERIFIED

    # But cataloged is blocked.
    with pytest.raises(GuardsFailed) as exc:
        advance_to(conn, capability_version_id=cv_id,
                    target=LifecycleState.CATALOGED)
    assert "summary" in {f.guard_name for f in exc.value.failures}


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

def test_candidate_cannot_jump_straight_to_cataloged(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    assert current_state(conn, cv_id) == LifecycleState.CANDIDATE
    with pytest.raises(IllegalTransition, match="expected 'verified'"):
        advance_to(conn, capability_version_id=cv_id,
                    target=LifecycleState.CATALOGED)


def test_terminal_states_are_not_reachable_via_advance_to(conn, registry):
    """quarantined/deprecated/revoked are set by explicit workflows, not
    the forward-transition function."""
    cv_id = _prepare_fully(conn, registry)
    for state in (LifecycleState.STALE, LifecycleState.QUARANTINED,
                   LifecycleState.DEPRECATED, LifecycleState.REVOKED):
        with pytest.raises(IllegalTransition, match="not reachable"):
            advance_to(conn, capability_version_id=cv_id, target=state)


def test_advancing_to_current_state_is_noop(conn, registry):
    cv_id = _prepare_fully(conn, registry)
    outcome = advance_to(conn, capability_version_id=cv_id,
                          target=LifecycleState.CANDIDATE)
    assert outcome.was_noop is True
    # No state_transition row was written.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM state_transition "
            "WHERE entity_kind = 'capability_version' AND entity_id = %s",
            (str(cv_id),),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Integration: risk_acceptance unblocks the guards-fail path
# ---------------------------------------------------------------------------

def test_risk_acceptance_lets_a_blocked_publication_proceed(
    conn, registry
):
    """
    A GPL repo normally can't reach cataloged (no_blocking_gate fails).
    Accepting the license risk (Step 10) makes can_publish return True,
    which makes the no_blocking_gate guard pass, which lets advance_to
    proceed to cataloged.
    """
    gpl_files = {**_HAPPY_FILES, "LICENSE": (
        b"                    GNU GENERAL PUBLIC LICENSE\n"
        b"                       Version 3, 29 June 2007\n\n"
    )}
    cv_id = _prepare_fully(
        conn, registry, external_key="acme/gpl", revision_key="sha1",
        files=gpl_files,
    )
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.ANALYZED)
    advance_to(conn, capability_version_id=cv_id,
                target=LifecycleState.VERIFIED)
    conn.commit()

    # Without acceptance, cataloged is blocked.
    with pytest.raises(GuardsFailed):
        advance_to(conn, capability_version_id=cv_id,
                    target=LifecycleState.CATALOGED)

    # Accept the license risk.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM gate_result WHERE capability_version_id = %s "
            "AND gate_name = 'license_compatibility'",
            (str(cv_id),),
        )
        gate_id = cur.fetchone()[0]
    accept_risk(
        conn, gate_result_id=gate_id,
        accepted_by="alice",
        reason="internal use only; legal reviewed GPL implications",
    )
    conn.commit()

    # Now it can proceed.
    outcome = advance_to(
        conn, capability_version_id=cv_id, target=LifecycleState.CATALOGED
    )
    conn.commit()
    assert outcome.new_state == "cataloged"
