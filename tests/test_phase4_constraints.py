"""
Phase 4 done-when:

  "Every project_constraint kind has an evaluator; evaluate() reports
   per-constraint verdicts and a hard_fail flag; browse_components
   with a project_id filters out hard-failing rows and annotates the
   survivors with their per-constraint verdicts."
"""
from __future__ import annotations

import json
import uuid

import pytest

from core.policy import constraints as pc
from mcp_server import queries as q


def _m(**overrides) -> pc.ComponentMaterial:
    base = {
        "component_kind": "library",
        "runtime": "python_import",
        "cost_tier": "free",
        "license_spdx": "MIT",
        "metadata": {},
    }
    base.update(overrides)
    return pc.ComponentMaterial.from_row(base)


def _cs(*pairs) -> list[pc.Constraint]:
    return [pc.Constraint(kind=k, detail=(d or {})) for (k, d) in pairs]


# ---------------------------------------------------------------------------
# cpu_only / no_gpu
# ---------------------------------------------------------------------------

def test_cpu_only_passes_when_no_gpu_hints():
    result = pc.evaluate(_cs(("cpu_only", None)), _m())
    assert result.hard_fail is False
    assert result.verdicts[0].passed


def test_cpu_only_fails_on_requires_gpu_metadata_flag():
    result = pc.evaluate(_cs(("cpu_only", None)),
                          _m(metadata={"requires_gpu": True}))
    assert result.hard_fail is True
    assert result.verdicts[0].reason == "gpu_required"


def test_cpu_only_fails_on_gpu_topic_tag():
    result = pc.evaluate(_cs(("no_gpu", None)),
                          _m(metadata={"topics": ["ffmpeg", "cuda", "video"]}))
    assert result.hard_fail is True
    assert result.verdicts[0].reason == "gpu_topic"


# ---------------------------------------------------------------------------
# must_be_free_tier
# ---------------------------------------------------------------------------

def test_free_tier_fails_on_paid():
    r = pc.evaluate(_cs(("must_be_free_tier", None)), _m(cost_tier="paid"))
    assert r.hard_fail is True
    assert r.verdicts[0].reason == "paid"


def test_free_tier_passes_on_free_and_free_tier():
    assert pc.evaluate(_cs(("must_be_free_tier", None)),
                        _m(cost_tier="free")).hard_fail is False
    assert pc.evaluate(_cs(("must_be_free_tier", None)),
                        _m(cost_tier="free_tier")).hard_fail is False


def test_free_tier_passes_on_unknown_but_flags_it():
    r = pc.evaluate(_cs(("must_be_free_tier", None)), _m(cost_tier=None))
    assert r.hard_fail is False
    assert r.verdicts[0].reason == "unknown_cost"


# ---------------------------------------------------------------------------
# must_be_local / always_off_at_night
# ---------------------------------------------------------------------------

def test_must_be_local_fails_on_http_endpoint():
    r = pc.evaluate(_cs(("must_be_local", None)), _m(runtime="http_endpoint"))
    assert r.hard_fail is True


def test_must_be_local_passes_on_python_import():
    r = pc.evaluate(_cs(("must_be_local", None)), _m(runtime="python_import"))
    assert r.hard_fail is False


def test_always_off_fails_on_remote_runtime():
    r = pc.evaluate(_cs(("always_off_at_night", None)),
                     _m(runtime="mcp_sse"))
    assert r.hard_fail is True


def test_always_off_fails_on_broker_topic():
    r = pc.evaluate(_cs(("always_off_at_night", None)),
                     _m(metadata={"topics": ["celery", "workers"]}))
    assert r.hard_fail is True


# ---------------------------------------------------------------------------
# budget_monthly_ceiling
# ---------------------------------------------------------------------------

def test_budget_ceiling_fails_over_amount():
    r = pc.evaluate(
        _cs(("budget_monthly_ceiling", {"amount_usd": 20})),
        _m(metadata={"monthly_cost_usd": 30}),
    )
    assert r.hard_fail is True
    assert r.verdicts[0].reason == "over_budget"


def test_budget_ceiling_passes_under_amount():
    r = pc.evaluate(
        _cs(("budget_monthly_ceiling", {"amount_usd": 50})),
        _m(metadata={"monthly_cost_usd": 15}),
    )
    assert r.hard_fail is False


def test_budget_ceiling_passes_when_cost_unknown():
    r = pc.evaluate(_cs(("budget_monthly_ceiling", {"amount_usd": 20})), _m())
    assert r.hard_fail is False
    assert r.verdicts[0].reason == "unknown_cost"


# ---------------------------------------------------------------------------
# license_allowlist / license_denylist
# ---------------------------------------------------------------------------

def test_license_allowlist_passes_matching_license():
    r = pc.evaluate(
        _cs(("license_allowlist", {"spdx_ids": ["MIT", "Apache-2.0"]})),
        _m(license_spdx="MIT"),
    )
    assert r.hard_fail is False


def test_license_allowlist_fails_non_matching():
    r = pc.evaluate(
        _cs(("license_allowlist", {"spdx_ids": ["MIT"]})),
        _m(license_spdx="AGPL-3.0"),
    )
    assert r.hard_fail is True
    assert r.verdicts[0].reason == "license_not_allowed"


def test_license_allowlist_passes_unknown_license():
    r = pc.evaluate(
        _cs(("license_allowlist", {"spdx_ids": ["MIT"]})),
        _m(license_spdx=None),
    )
    assert r.hard_fail is False
    assert r.verdicts[0].reason == "unknown_license"


def test_license_denylist_fails_on_denied():
    r = pc.evaluate(
        _cs(("license_denylist", {"spdx_ids": ["AGPL-3.0"]})),
        _m(license_spdx="AGPL-3.0"),
    )
    assert r.hard_fail is True


def test_license_denylist_case_insensitive():
    r = pc.evaluate(
        _cs(("license_denylist", {"spdx_ids": ["agpl-3.0"]})),
        _m(license_spdx="AGPL-3.0"),
    )
    assert r.hard_fail is True


# ---------------------------------------------------------------------------
# runtime_allowlist / component_kind_allowlist
# ---------------------------------------------------------------------------

def test_runtime_allowlist():
    ok = pc.evaluate(
        _cs(("runtime_allowlist", {"runtimes": ["python_import", "mcp_stdio"]})),
        _m(runtime="python_import"),
    )
    bad = pc.evaluate(
        _cs(("runtime_allowlist", {"runtimes": ["python_import"]})),
        _m(runtime="git_clone"),
    )
    assert ok.hard_fail is False
    assert bad.hard_fail is True


def test_component_kind_allowlist():
    r = pc.evaluate(
        _cs(("component_kind_allowlist", {"kinds": ["library", "repo"]})),
        _m(component_kind="mcp_tool"),
    )
    assert r.hard_fail is True


# ---------------------------------------------------------------------------
# Multiple constraints — any hard-fail hard-fails the whole result
# ---------------------------------------------------------------------------

def test_multiple_constraints_all_pass():
    r = pc.evaluate(
        _cs(("must_be_free_tier", None),
            ("must_be_local", None),
            ("license_allowlist", {"spdx_ids": ["MIT"]})),
        _m(),
    )
    assert r.hard_fail is False


def test_multiple_constraints_one_fail_hard_fails_all():
    r = pc.evaluate(
        _cs(("must_be_free_tier", None), ("must_be_local", None)),
        _m(cost_tier="free", runtime="http_endpoint"),
    )
    assert r.hard_fail is True
    assert "must_be_local:remote_runtime" in r.fail_reasons


def test_unknown_constraint_kind_passes_with_note():
    r = pc.evaluate(_cs(("mystery_constraint", None)), _m())
    assert r.hard_fail is False
    assert r.verdicts[0].reason == "unknown_constraint_kind"


# ---------------------------------------------------------------------------
# Integration through queries — project_id path
# ---------------------------------------------------------------------------

def _mk_project(conn, constraints: list[tuple[str, dict]]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project (name) VALUES (%s) RETURNING id::text",
            (f"p-{uuid.uuid4().hex[:6]}",),
        )
        pid = cur.fetchone()[0]
        for kind, detail in constraints:
            cur.execute(
                "INSERT INTO project_constraint (project_id, kind, detail) "
                "VALUES (%s, %s, %s::jsonb)",
                (pid, kind, json.dumps(detail or {})),
            )
    return pid


def _mk_component(conn, key, **fields) -> str:
    with conn.cursor() as cur:
        row = {
            "normalized_key": key, "display_name": key,
            "ecosystem": "source", "kind": "library",
            "component_kind": "library", "runtime": "python_import",
            "cost_tier": "free", "license_spdx": "MIT",
            "metadata": "{}",
        }
        row.update({k: (json.dumps(v) if k == "metadata" else v)
                    for k, v in fields.items()})
        cur.execute(
            "INSERT INTO capability "
            "(normalized_key, display_name, ecosystem, kind, "
            " component_kind, runtime, cost_tier, license_spdx, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
            "RETURNING id::text",
            (row["normalized_key"], row["display_name"], row["ecosystem"],
             row["kind"], row["component_kind"], row["runtime"],
             row["cost_tier"], row["license_spdx"], row["metadata"]),
        )
        return cur.fetchone()[0]


def test_browse_with_project_id_hard_filters_failing_rows(conn):
    _mk_component(conn, "pypi:free-mit", cost_tier="free", license_spdx="MIT")
    _mk_component(conn, "pypi:paid-mit", cost_tier="paid", license_spdx="MIT")
    _mk_component(conn, "pypi:free-agpl", cost_tier="free", license_spdx="AGPL-3.0")
    project = _mk_project(conn, [
        ("must_be_free_tier", None),
        ("license_denylist", {"spdx_ids": ["AGPL-3.0"]}),
    ])
    conn.commit()

    rows = q.browse_components(conn, project_id=project)
    keys = {r["normalized_key"] for r in rows}
    assert "pypi:free-mit" in keys
    assert "pypi:paid-mit" not in keys
    assert "pypi:free-agpl" not in keys


def test_browse_survivors_carry_constraint_verdicts(conn):
    _mk_component(conn, "pypi:winner", cost_tier="free", license_spdx="MIT")
    project = _mk_project(conn, [("must_be_free_tier", None)])
    conn.commit()

    rows = q.browse_components(conn, project_id=project)
    assert rows[0]["constraint_verdicts"][0]["kind"] == "must_be_free_tier"
    assert rows[0]["constraint_verdicts"][0]["passed"] is True


def test_browse_with_no_project_id_returns_all(conn):
    _mk_component(conn, "pypi:a", cost_tier="paid")
    _mk_component(conn, "pypi:b", cost_tier="free")
    conn.commit()

    rows = q.browse_components(conn)
    assert {r["normalized_key"] for r in rows} == {"pypi:a", "pypi:b"}


def test_browse_with_project_that_has_no_constraints_returns_all(conn):
    _mk_component(conn, "pypi:x", cost_tier="paid")
    project = _mk_project(conn, [])
    conn.commit()

    rows = q.browse_components(conn, project_id=project)
    assert len(rows) == 1


def test_capability_constraint_fit_returns_full_verdict(conn):
    cid = _mk_component(conn, "pypi:target", cost_tier="paid", license_spdx="AGPL-3.0")
    project = _mk_project(conn, [
        ("must_be_free_tier", None),
        ("license_denylist", {"spdx_ids": ["AGPL-3.0"]}),
    ])
    conn.commit()

    fit = q.capability_constraint_fit(conn, project_id=project, capability_id=cid)
    assert fit is not None
    assert fit["hard_fail"] is True
    kinds = {v["kind"] for v in fit["verdicts"]}
    assert kinds == {"must_be_free_tier", "license_denylist"}
    fails = {v["kind"] for v in fit["verdicts"] if not v["passed"]}
    assert fails == {"must_be_free_tier", "license_denylist"}


def test_capability_constraint_fit_returns_none_for_bad_capability_id(conn):
    project = _mk_project(conn, [])
    conn.commit()
    assert q.capability_constraint_fit(conn, project, "not-a-uuid") is None
    assert q.capability_constraint_fit(conn, project, str(uuid.uuid4())) is None
