"""
DB queries backing the MCP tools. Pure functions of a psycopg connection —
no MCP types leak in, so these are testable without the MCP transport.

Every dict returned here is safe to hand straight to json.dumps.

Vocabulary note: `capability.kind` (pre-Phase-1) is the semantic role of
what the thing does — 'library' | 'cli' | 'service' | .... The new
`capability.component_kind` (Phase 1) is the *format* — 'library' | 'repo'
| 'agent' | 'skill' | 'mcp_tool' | 'workflow_template'. Both filters
exist because they answer different questions ("what does it do" vs
"what shape does it come in"). The overlap on 'library' is unavoidable
but the names disambiguate.
"""
from __future__ import annotations

import uuid
from typing import Any


# ---------------------------------------------------------------------------
# search_capabilities
# ---------------------------------------------------------------------------

def search_capabilities(
    conn,
    query: str,
    ecosystem: str | None = None,
    capability_kind: str | None = None,
    component_kind: str | None = None,
    runtime: str | None = None,
    cost_tier: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Case-insensitive substring search over display_name and normalized_key,
    joined to the head (non-superseded) version and its scorecard when one
    exists. Returns the top `limit` rows ordered by intrinsic score desc,
    display_name asc.

    Filters (all optional, ANDed together):
      ecosystem       — 'pypi', 'npm', 'source', ...
      capability_kind — semantic role ('library', 'cli', 'service', ...)
      component_kind  — format ('library', 'repo', 'agent', 'skill',
                        'mcp_tool', 'workflow_template')
      runtime         — 'python_import', 'mcp_stdio', 'claude_skill', ...
      cost_tier       — 'free', 'free_tier', 'cheap_paid', 'paid'
    """
    q = (query or "").strip()
    if not q:
        return []
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    sql = """
        SELECT
            c.id::text          AS id,
            c.normalized_key,
            c.display_name,
            c.ecosystem,
            c.kind              AS capability_kind,
            c.component_kind,
            c.runtime,
            c.cost_tier,
            c.license_spdx,
            v.id::text          AS head_version_id,
            v.display_version,
            s.total_score,
            s.confidence
        FROM capability c
        LEFT JOIN LATERAL (
            SELECT id, display_version
            FROM capability_version
            WHERE capability_id = c.id
              AND superseded_by_id IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) v ON TRUE
        LEFT JOIN LATERAL (
            SELECT total_score, confidence
            FROM scorecard
            WHERE capability_version_id = v.id
            ORDER BY computed_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE (
                c.display_name ILIKE %(pat)s
             OR c.normalized_key ILIKE %(pat)s
        )
          AND (%(ecosystem)s::text       IS NULL OR c.ecosystem       = %(ecosystem)s)
          AND (%(capability_kind)s::text IS NULL OR c.kind            = %(capability_kind)s)
          AND (%(component_kind)s::text  IS NULL OR c.component_kind  = %(component_kind)s)
          AND (%(runtime)s::text         IS NULL OR c.runtime         = %(runtime)s)
          AND (%(cost_tier)s::text       IS NULL OR c.cost_tier       = %(cost_tier)s)
        ORDER BY
            COALESCE(s.total_score, 0) DESC,
            c.display_name ASC
        LIMIT %(limit)s
    """
    params = {
        "pat": f"%{q}%",
        "ecosystem": ecosystem,
        "capability_kind": capability_kind,
        "component_kind": component_kind,
        "runtime": runtime,
        "cost_tier": cost_tier,
        "limit": limit,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [_json_safe(row) for row in rows]


# ---------------------------------------------------------------------------
# browse_components
# ---------------------------------------------------------------------------

def browse_components(
    conn,
    component_kind: str | None = None,
    ecosystem: str | None = None,
    runtime: str | None = None,
    cost_tier: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Enumerate components without a keyword. This is the discovery surface
    — the caller doesn't know what to search for, they want to see what
    exists in a category.

    All filters optional. With no filters, returns the top `limit`
    components across the whole registry, ranked by intrinsic score
    (unscored last).
    """
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500

    sql = """
        SELECT
            c.id::text          AS id,
            c.normalized_key,
            c.display_name,
            c.ecosystem,
            c.kind              AS capability_kind,
            c.component_kind,
            c.runtime,
            c.cost_tier,
            c.license_spdx,
            v.id::text          AS head_version_id,
            v.display_version,
            s.total_score,
            s.confidence
        FROM capability c
        LEFT JOIN LATERAL (
            SELECT id, display_version
            FROM capability_version
            WHERE capability_id = c.id
              AND superseded_by_id IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) v ON TRUE
        LEFT JOIN LATERAL (
            SELECT total_score, confidence
            FROM scorecard
            WHERE capability_version_id = v.id
            ORDER BY computed_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE
              (%(component_kind)s::text IS NULL OR c.component_kind = %(component_kind)s)
          AND (%(ecosystem)s::text      IS NULL OR c.ecosystem      = %(ecosystem)s)
          AND (%(runtime)s::text        IS NULL OR c.runtime        = %(runtime)s)
          AND (%(cost_tier)s::text      IS NULL OR c.cost_tier      = %(cost_tier)s)
        ORDER BY
            COALESCE(s.total_score, 0) DESC,
            c.display_name ASC
        LIMIT %(limit)s
    """
    params = {
        "component_kind": component_kind,
        "ecosystem": ecosystem,
        "runtime": runtime,
        "cost_tier": cost_tier,
        "limit": limit,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [_json_safe(row) for row in rows]


# ---------------------------------------------------------------------------
# capability_detail
# ---------------------------------------------------------------------------

def capability_detail(conn, capability_id: str) -> dict[str, Any] | None:
    """
    Full record for one capability: metadata (including component_kind,
    runtime, cost_tier, license_spdx), head version, interfaces (with
    input/output type descriptors when populated), declared dependencies,
    and the head version's scorecard (if any). Returns None if the
    capability is not found.
    """
    try:
        cap_uuid = uuid.UUID(capability_id)
    except (ValueError, TypeError):
        return None

    cap = _fetch_capability(conn, cap_uuid)
    if cap is None:
        return None

    head = _fetch_head_version(conn, cap_uuid)
    interfaces: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    scorecard: dict[str, Any] | None = None
    if head is not None:
        interfaces = _fetch_interfaces(conn, head["id"])
        dependencies = _fetch_dependencies(conn, head["id"])
        scorecard = _fetch_scorecard(conn, head["id"])

    return _json_safe({
        "id": str(cap_uuid),
        "normalized_key": cap["normalized_key"],
        "display_name": cap["display_name"],
        "ecosystem": cap["ecosystem"],
        "capability_kind": cap["capability_kind"],
        "component_kind": cap["component_kind"],
        "runtime": cap["runtime"],
        "cost_tier": cap["cost_tier"],
        "license_spdx": cap["license_spdx"],
        "first_seen_at": cap["first_seen_at"],
        "head_version": head,
        "interfaces": interfaces,
        "dependencies": dependencies,
        "scorecard": scorecard,
    })


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_capability(conn, cap_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_key, display_name, ecosystem, kind, "
            "       component_kind, runtime, cost_tier, license_spdx, "
            "       first_seen_at "
            "FROM capability WHERE id = %s",
            (cap_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "normalized_key": row[0],
        "display_name": row[1],
        "ecosystem": row[2],
        "capability_kind": row[3],
        "component_kind": row[4],
        "runtime": row[5],
        "cost_tier": row[6],
        "license_spdx": row[7],
        "first_seen_at": row[8],
    }


def _fetch_head_version(conn, cap_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, version_key, version_kind, display_version, created_at "
            "FROM capability_version "
            "WHERE capability_id = %s AND superseded_by_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (cap_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "version_key": row[1],
        "version_kind": row[2],
        "display_version": row[3],
        "created_at": row[4],
    }


def _fetch_interfaces(conn, version_id: uuid.UUID) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, name, signature, language, input_type, output_type, "
            "       evidence_item_id "
            "FROM capability_interface "
            "WHERE capability_version_id = %s "
            "ORDER BY kind, name",
            (version_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "kind": r[0],
            "name": r[1],
            "signature": r[2],
            "language": r[3],
            "input_type": r[4],
            "output_type": r[5],
            "evidence_item_id": str(r[6]),
        }
        for r in rows
    ]


def _fetch_dependencies(conn, version_id: uuid.UUID) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT depends_on_ecosystem, depends_on_name, version_spec, dep_kind, "
            "       evidence_item_id "
            "FROM capability_dependency "
            "WHERE capability_version_id = %s "
            "ORDER BY depends_on_ecosystem, depends_on_name",
            (version_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "ecosystem": r[0],
            "name": r[1],
            "version_spec": r[2],
            "kind": r[3],
            "evidence_item_id": str(r[4]),
        }
        for r in rows
    ]


def _fetch_scorecard(conn, version_id: uuid.UUID) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, total_score, confidence, computed_hash, computed_at "
            "FROM scorecard "
            "WHERE capability_version_id = %s "
            "ORDER BY computed_at DESC LIMIT 1",
            (version_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        scorecard_id = row[0]
        card = {
            "total_score": row[1],
            "confidence": row[2],
            "computed_hash": row[3],
            "computed_at": row[4],
        }
        cur.execute(
            "SELECT dimension_name, raw_score, weight, weighted_score, "
            "       coverage, contradiction, evidence_count "
            "FROM score_dimension_result "
            "WHERE scorecard_id = %s "
            "ORDER BY dimension_name",
            (scorecard_id,),
        )
        dims = [
            {
                "name": d[0],
                "raw_score": d[1],
                "weight": d[2],
                "weighted_score": d[3],
                "coverage": d[4],
                "contradiction": d[5],
                "evidence_count": d[6],
            }
            for d in cur.fetchall()
        ]
    card["dimensions"] = dims
    return card


# ---------------------------------------------------------------------------
# JSON-safety pass: UUIDs, Decimals, datetimes → primitives
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    import datetime
    import decimal
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return obj
