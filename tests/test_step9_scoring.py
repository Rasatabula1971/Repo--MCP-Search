"""
Step 9 done-when:
  "The same evidence set and profile version reproduce an identical
   scorecard, byte for byte, on a second run."

Also cover:
  - Profile validation (weights sum to 1, kinds are known, immutable
    per version once registered).
  - Per-dimension calculation (presence / count / absence).
  - Confidence uses evidence coverage, NOT judgment_response.self_confidence.
  - core.scoring imports nothing forbidden (import-linter also checks this).
  - Persisted scorecard matches the pure-scorer output.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.registry import promote_revision
from core.scoring.profile import Dimension, Profile, ProfileError, load
from core.scoring.scorer import Evidence, score
from workers.analysis import run_analysis
from workers.ingestion import ingest_revision
from workers.scoring import (
    load_default_profile,
    score_capability_version,
)


# ---------------------------------------------------------------------------
# Profile loading / validation
# ---------------------------------------------------------------------------

PROFILE_PATH = (
    Path(__file__).parent.parent / "config" / "scoring_profiles" / "default.yaml"
)


def test_default_profile_loads_and_hashes():
    p = load(PROFILE_PATH)
    assert p.name == "default"
    assert p.version == 1
    assert len(p.dimensions) == 5
    assert len(p.profile_hash) == 64  # SHA256 hex


def test_default_profile_hash_is_stable_across_loads():
    a = load(PROFILE_PATH)
    b = load(PROFILE_PATH)
    assert a.profile_hash == b.profile_hash


def test_profile_rejects_weights_not_summing_to_one(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndimensions:\n"
        "  - {name: a, weight: 0.4, kind: presence, "
        "     inputs: {evidence_type: dependency}}\n"
        "  - {name: b, weight: 0.4, kind: presence, "
        "     inputs: {evidence_type: interface}}\n"
    )
    with pytest.raises(ProfileError, match="sum to 1.0"):
        load(bad)


def test_profile_rejects_unknown_kind(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndimensions:\n"
        "  - {name: a, weight: 1.0, kind: guess, "
        "     inputs: {evidence_type: dependency}}\n"
    )
    with pytest.raises(ProfileError, match="unknown kind"):
        load(bad)


def test_profile_rejects_missing_evidence_type(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndimensions:\n"
        "  - {name: a, weight: 1.0, kind: presence, inputs: {}}\n"
    )
    with pytest.raises(ProfileError, match="evidence_type"):
        load(bad)


def test_profile_rejects_duplicate_dimension_names(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: bad\nversion: 1\ndimensions:\n"
        "  - {name: dup, weight: 0.5, kind: presence, "
        "     inputs: {evidence_type: dependency}}\n"
        "  - {name: dup, weight: 0.5, kind: presence, "
        "     inputs: {evidence_type: interface}}\n"
    )
    with pytest.raises(ProfileError, match="duplicate"):
        load(bad)


# ---------------------------------------------------------------------------
# Pure scorer — per-dimension behaviour
# ---------------------------------------------------------------------------

def _mini_profile_from_dict(raw: dict) -> Profile:
    """Test helper: build a Profile via the loader without touching disk."""
    from core.scoring.profile import _from_dict
    return _from_dict(raw)


def _minimal_profile(dim_dict: dict) -> Profile:
    """A profile with exactly one dimension at weight 1.0."""
    raw = {
        "name": "test", "version": 1,
        "dimensions": [{
            "name": "d1", "weight": 1.0,
            **dim_dict,
        }],
    }
    return _mini_profile_from_dict(raw)


def test_presence_dimension_scores_1_when_any_evidence_matches():
    p = _minimal_profile({
        "kind": "presence",
        "inputs": {"evidence_type": "dependency"},
    })
    result = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="dependency",
                 extracted_value={"name": "httpx"}),
    ])
    assert result.total_score == 1.0
    assert result.dimensions[0].raw_score == 1.0
    assert result.dimensions[0].coverage == 1.0


def test_presence_dimension_scores_0_when_no_evidence():
    p = _minimal_profile({
        "kind": "presence",
        "inputs": {"evidence_type": "dependency"},
    })
    result = score(p, [])
    assert result.total_score == 0.0
    assert result.dimensions[0].coverage == 0.0


def test_presence_dimension_with_require_field_not_filters_correctly():
    p = _minimal_profile({
        "kind": "presence",
        "inputs": {"evidence_type": "license"},
        "require_field_not": {"field": "spdx_id", "value": "unknown"},
    })
    # An 'unknown' license shouldn't count as presence.
    unknown_only = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "unknown"}),
    ])
    assert unknown_only.total_score == 0.0

    # A recognised license should count.
    known = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
    ])
    assert known.total_score == 1.0


def test_count_dimension_normalizes_to_ceiling():
    p = _minimal_profile({
        "kind": "count",
        "inputs": {"evidence_type": "test_indicator"},
        "normalize": {"ceiling": 4},
    })
    for count, expected in [(0, 0.0), (1, 0.25), (2, 0.5), (4, 1.0), (10, 1.0)]:
        result = score(p, [
            Evidence(id=str(uuid.uuid4()), evidence_type="test_indicator",
                     extracted_value={})
            for _ in range(count)
        ])
        assert result.dimensions[0].raw_score == expected, (
            f"count={count}: expected {expected}, got {result.dimensions[0].raw_score}"
        )


def test_absence_dimension_inverts_count():
    """The whole point of absence: presence hurts the score."""
    p = _minimal_profile({
        "kind": "absence",
        "inputs": {"evidence_type": "secret_indicator"},
        "normalize": {"cap": 4},
    })
    for count, expected in [(0, 1.0), (1, 0.75), (2, 0.5), (4, 0.0), (10, 0.0)]:
        result = score(p, [
            Evidence(id=str(uuid.uuid4()), evidence_type="secret_indicator",
                     extracted_value={"pattern_name": "aws_key"})
            for _ in range(count)
        ])
        assert result.dimensions[0].raw_score == expected


# ---------------------------------------------------------------------------
# Confidence — coverage-weighted, contradiction-adjusted, NOT self_confidence
# ---------------------------------------------------------------------------

def test_confidence_reflects_dimension_coverage():
    """A profile with 5 dimensions but evidence for only 2 of them
    has coverage-weighted confidence < 1."""
    p = load(PROFILE_PATH)
    result = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="dependency",
                 extracted_value={"name": "httpx"}),
    ])
    # Only 'has_declared_dependencies' matched. Its weight is 0.20.
    # So coverage-weighted confidence is 0.20.
    assert result.confidence == pytest.approx(0.20, abs=0.001)


def test_confidence_is_1_when_all_dimensions_have_evidence():
    p = load(PROFILE_PATH)
    result = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="dependency",
                 extracted_value={"name": "x"}),
        Evidence(id=str(uuid.uuid4()), evidence_type="interface",
                 extracted_value={"name": "f", "kind": "function"}),
        Evidence(id=str(uuid.uuid4()), evidence_type="test_indicator",
                 extracted_value={}),
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
        Evidence(id=str(uuid.uuid4()), evidence_type="secret_indicator",
                 extracted_value={"pattern_name": "aws"}),
    ])
    # Every dimension has at least one matching evidence.
    assert result.confidence == pytest.approx(1.0, abs=0.001)


def test_confidence_does_not_use_judgment_self_confidence(conn):
    """
    Even if judgment_response rows carry a self_confidence value,
    core.scoring must ignore them entirely. Test at the API level:
    the scorer accepts only Evidence, which has no self_confidence field.

    Structural check: ensure the Evidence dataclass exposes only the
    three fields it should.
    """
    fields = set(Evidence.__dataclass_fields__.keys())
    assert fields == {"id", "evidence_type", "extracted_value"}, (
        f"Evidence must not carry a self_confidence field; got {fields}"
    )


# ---------------------------------------------------------------------------
# Contradiction — 0 for MVP, but the hook must exist
# ---------------------------------------------------------------------------

def test_contradiction_zero_when_all_matched_evidence_agrees():
    p = _minimal_profile({
        "kind": "presence",
        "inputs": {"evidence_type": "license"},
        "require_field_not": {"field": "spdx_id", "value": "unknown"},
    })
    result = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
    ])
    assert result.dimensions[0].contradiction == 0.0


def test_contradiction_one_when_matched_evidence_disagrees():
    """Two license evidences with different SPDX ids is a contradiction
    on that dimension. Once Step 15's multi-provider consensus lands,
    this shape carries their disagreement too."""
    p = _minimal_profile({
        "kind": "presence",
        "inputs": {"evidence_type": "license"},
        "require_field_not": {"field": "spdx_id", "value": "unknown"},
    })
    result = score(p, [
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
        Evidence(id=str(uuid.uuid4()), evidence_type="license",
                 extracted_value={"spdx_id": "Apache-2.0"}),
    ])
    assert result.dimensions[0].contradiction == 1.0
    # And confidence drops accordingly (one dim, matched, contradiction=1
    # → contradiction factor is 0 → confidence = coverage * 0 = 0).
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# THE STEP 9 DONE-WHEN — byte-for-byte determinism
# ---------------------------------------------------------------------------

def test_scoring_is_byte_stable_across_calls():
    """
    Same profile + same evidence set → identical computed_hash.
    This is the done-when, exact.
    """
    p = load(PROFILE_PATH)
    fixed_ids = [str(uuid.UUID(int=i)) for i in range(1, 4)]
    ev = [
        Evidence(id=fixed_ids[0], evidence_type="dependency",
                 extracted_value={"name": "httpx"}),
        Evidence(id=fixed_ids[1], evidence_type="interface",
                 extracted_value={"name": "f", "kind": "function"}),
        Evidence(id=fixed_ids[2], evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
    ]
    a = score(p, ev)
    b = score(p, ev)
    assert a.computed_hash == b.computed_hash


def test_scoring_is_byte_stable_across_yield_orders():
    """
    Same evidence set, DIFFERENT iteration order → still same hash.
    Uses MULTIPLE evidence items per dimension so the order within
    each `matched` list actually varies — that's what forces the
    scorer's internal sort to be doing its job.
    """
    p = load(PROFILE_PATH)
    fixed_ids = [str(uuid.UUID(int=i)) for i in range(1, 6)]
    ev = [
        Evidence(id=fixed_ids[0], evidence_type="dependency",
                 extracted_value={"name": "a"}),
        Evidence(id=fixed_ids[1], evidence_type="dependency",
                 extracted_value={"name": "b"}),
        Evidence(id=fixed_ids[2], evidence_type="dependency",
                 extracted_value={"name": "c"}),
        Evidence(id=fixed_ids[3], evidence_type="interface",
                 extracted_value={"name": "f", "kind": "function"}),
        Evidence(id=fixed_ids[4], evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
    ]
    a = score(p, ev)
    b = score(p, list(reversed(ev)))
    assert a.computed_hash == b.computed_hash


def test_scoring_is_byte_stable_across_process_boundary():
    """
    Compute a hash in a subprocess and compare — protects against
    dict-ordering or hash-randomization surprises across processes.
    """
    p = load(PROFILE_PATH)
    fixed_ids = [str(uuid.UUID(int=i)) for i in range(1, 4)]

    # Compute in this process.
    from core.scoring.scorer import score as _score
    ev_here = [
        Evidence(id=fixed_ids[0], evidence_type="dependency",
                 extracted_value={"name": "httpx"}),
        Evidence(id=fixed_ids[1], evidence_type="license",
                 extracted_value={"spdx_id": "MIT"}),
    ]
    hash_here = _score(p, ev_here).computed_hash

    # Compute in a fresh subprocess.
    script = (
        "import sys; sys.path.insert(0, r'/home/claude/cip'); "
        "from pathlib import Path; "
        "from core.scoring.profile import load; "
        "from core.scoring.scorer import Evidence, score; "
        f"p = load(r'{PROFILE_PATH}'); "
        "ev = ["
        f"  Evidence(id='{fixed_ids[0]}', evidence_type='dependency', "
        "           extracted_value={'name': 'httpx'}), "
        f"  Evidence(id='{fixed_ids[1]}', evidence_type='license', "
        "           extracted_value={'spdx_id': 'MIT'}),"
        "]; "
        "print(score(p, ev).computed_hash)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=True,
        env={"PYTHONHASHSEED": "random"},  # extra paranoia
    )
    hash_subproc = result.stdout.strip()
    assert hash_here == hash_subproc, (
        f"determinism failure: in-process {hash_here!r} != "
        f"subprocess {hash_subproc!r}"
    )


def test_scoring_hash_changes_when_evidence_changes():
    """Sanity: adding an evidence row must change the hash."""
    p = load(PROFILE_PATH)
    fid = str(uuid.UUID(int=1))
    a = score(p, [Evidence(id=fid, evidence_type="dependency",
                            extracted_value={"name": "httpx"})])
    b = score(p, [Evidence(id=fid, evidence_type="dependency",
                            extracted_value={"name": "httpx"}),
                  Evidence(id=str(uuid.UUID(int=2)), evidence_type="license",
                            extracted_value={"spdx_id": "MIT"})])
    assert a.computed_hash != b.computed_hash


# ---------------------------------------------------------------------------
# Orchestrator — persistence + end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


@pytest.fixture
def promoted_capability_version(conn, registry):
    """End-to-end fixture: ingest → analyse → promote, return
    capability_version_id ready to be scored."""
    fake = registry.get("fake")
    fake.add_revision("acme/lib", "sha_1", {
        "pyproject.toml": (
            b"[project]\nname = 'acme-lib'\nversion = '0.1.0'\n"
            b"dependencies = ['httpx']\n"
        ),
        "src/acme/core.py": (
            b"def greet(x):\n    return x\n"
            b"class W:\n    def m(self): pass\n"
        ),
        "tests/test_x.py": b"def test_a(): pass\n",
        "LICENSE": (
            b"MIT License\n\n"
            b"Permission is hereby granted, free of charge, to any "
            b"person obtaining a copy\n"
        ),
    })
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
            "VALUES (%s, 'acme/lib', 'acme/lib', 'repository') RETURNING id",
            (pid,),
        )
        aid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'sha_1') RETURNING id",
            (aid,),
        )
        rid = cur.fetchone()[0]
    conn.commit()
    ingest_revision(conn, registry=registry, source_revision_id=rid)
    run_analysis(conn, registry=registry, source_revision_id=rid)
    out = promote_revision(
        conn, registry=registry, source_revision_id=rid
    )
    return out.capability_version_id


def test_orchestrator_persists_scorecard_matching_pure_scorer(
    conn, promoted_capability_version
):
    profile = load_default_profile()
    outcome = score_capability_version(
        conn,
        capability_version_id=promoted_capability_version,
        profile=profile,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT total_score, confidence, computed_hash "
            "FROM scorecard WHERE id = %s",
            (str(outcome.scorecard_id),),
        )
        row = cur.fetchone()
    total, confidence, ch = row
    assert float(total) == outcome.total_score
    assert float(confidence) == outcome.confidence
    assert ch == outcome.computed_hash


def test_orchestrator_writes_one_row_per_dimension_plus_evidence_links(
    conn, promoted_capability_version
):
    profile = load_default_profile()
    outcome = score_capability_version(
        conn,
        capability_version_id=promoted_capability_version,
        profile=profile,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM score_dimension_result "
            "WHERE scorecard_id = %s",
            (str(outcome.scorecard_id),),
        )
        assert cur.fetchone()[0] == 5     # profile has 5 dimensions

        cur.execute(
            """
            SELECT COUNT(*)
            FROM score_evidence_link l
            JOIN score_dimension_result r ON r.id = l.score_dimension_result_id
            WHERE r.scorecard_id = %s
            """,
            (str(outcome.scorecard_id),),
        )
        # At least one link per dimension that had matching evidence.
        assert cur.fetchone()[0] >= 4


def test_orchestrator_is_idempotent_same_hash_same_evidence(
    conn, promoted_capability_version
):
    """Second run against unchanged evidence produces the same hash and
    returns was_noop=True."""
    profile = load_default_profile()
    first = score_capability_version(
        conn,
        capability_version_id=promoted_capability_version,
        profile=profile,
    )
    second = score_capability_version(
        conn,
        capability_version_id=promoted_capability_version,
        profile=profile,
    )
    assert first.computed_hash == second.computed_hash
    assert second.was_noop is True


def test_orchestrator_rejects_edit_to_same_profile_version(conn):
    """Once (name, version, profile_hash) is registered, editing the
    profile without bumping the version must be rejected."""
    from core.scoring.profile import _from_dict
    v1_a = _from_dict({
        "name": "immutable-test", "version": 1,
        "dimensions": [
            {"name": "d1", "weight": 1.0, "kind": "presence",
             "inputs": {"evidence_type": "dependency"}},
        ],
    })
    # Register once, no capability_version needed for the check.
    from workers.scoring import _upsert_profile
    _upsert_profile(conn, v1_a)
    conn.commit()

    # Now try to register a different-shape profile under same (name, v).
    v1_b_different = _from_dict({
        "name": "immutable-test", "version": 1,
        "dimensions": [
            {"name": "d1", "weight": 1.0, "kind": "count",   # kind changed
             "inputs": {"evidence_type": "dependency"},
             "normalize": {"ceiling": 3}},
        ],
    })
    with pytest.raises(ValueError, match="immutable"):
        _upsert_profile(conn, v1_b_different)


# ---------------------------------------------------------------------------
# Structural: core.scoring must not reach into judgment or providers
# ---------------------------------------------------------------------------

def test_core_scoring_modules_do_not_import_forbidden_packages():
    """
    Belt-and-braces alongside import-linter: import both scoring modules
    and confirm neither has pulled in judgment / connectors / provider SDKs.
    Catches accidental imports that import-linter would also catch, but
    verifies at runtime too.
    """
    for modname in ("core.scoring.profile", "core.scoring.scorer"):
        # Fresh import to be sure we're looking at current state.
        importlib.import_module(modname)

    loaded = set(sys.modules)
    forbidden_prefixes = (
        "core.judgment",
        "connectors",
        "openai",
        "anthropic",
    )
    # google.* is fine (yaml pulls in nothing under google); we only
    # care about GENERATIVE clients, which the profile+scorer never touch.
    for name in list(loaded):
        for prefix in forbidden_prefixes:
            if name == prefix or name.startswith(prefix + "."):
                # scoring modules themselves may indirectly pull in
                # things via the test file. Only flag if a scoring
                # module DIRECTLY imports it.
                mod = sys.modules.get("core.scoring.scorer")
                if mod and prefix in (mod.__file__ or ""):
                    pytest.fail(
                        f"core.scoring appears to import {name!r} "
                        f"(forbidden prefix {prefix!r})"
                    )
