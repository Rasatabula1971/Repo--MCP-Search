"""
CIP MCP server.

Exposes the capability registry as MCP tools an LLM can call. Uses the
official Python MCP SDK's FastMCP wrapper. Transport: stdio, which is
what Claude Code and Claude Desktop expect.

Registering with Claude Code:

    claude mcp add cip -- cip-mcp

Or, if the console script is not on PATH:

    claude mcp add cip -- python -m mcp_server.server

Environment: reads DATABASE_URL from .env in the current working
directory, same as everything else in this repo.
"""
from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from db.connection import connect
from mcp_server import queries


mcp = FastMCP("cip")


@mcp.tool()
def search_capabilities(
    query: str,
    ecosystem: Optional[str] = None,
    capability_kind: Optional[str] = None,
    component_kind: Optional[str] = None,
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Keyword search the CIP capability registry.

    Args:
      query: case-insensitive substring against display_name and normalized_key.
      ecosystem: optional — 'pypi', 'npm', 'source'.
      capability_kind: optional semantic role — 'library', 'cli', 'service', ...
      component_kind: optional format — 'library', 'repo', 'agent', 'skill',
        'mcp_tool', 'workflow_template'.
      runtime: optional — 'python_import', 'mcp_stdio', 'claude_skill',
        'git_clone', ...
      cost_tier: optional — 'free', 'free_tier', 'cheap_paid', 'paid'.
      project_id: optional UUID — when set, hard-filters rows that fail
        any of the project's constraints (cpu_only, must_be_free_tier,
        must_be_local, license_allowlist, etc.). Kept rows include a
        `constraint_verdicts` field showing per-constraint outcomes.
      limit: max rows (1-100, default 20).

    Returns rows with {id, normalized_key, display_name, ecosystem,
    capability_kind, component_kind, runtime, cost_tier, license_spdx,
    head_version_id, display_version, total_score, confidence}. Ranked by
    intrinsic score descending; unscored rows last.
    """
    conn = connect()
    try:
        return queries.search_capabilities(
            conn,
            query=query,
            ecosystem=ecosystem,
            capability_kind=capability_kind,
            component_kind=component_kind,
            runtime=runtime,
            cost_tier=cost_tier,
            project_id=project_id,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def browse_components(
    component_kind: Optional[str] = None,
    ecosystem: Optional[str] = None,
    runtime: Optional[str] = None,
    cost_tier: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Discovery tool: enumerate components without a keyword. Use when
    the caller doesn't know what to search for and wants to see what
    exists in a category.

    Args:
      component_kind: 'library' | 'repo' | 'agent' | 'skill' | 'mcp_tool'
        | 'workflow_template'.
      ecosystem: 'pypi', 'npm', 'source', ...
      runtime: 'python_import', 'mcp_stdio', 'claude_skill', ...
      cost_tier: 'free', 'free_tier', 'cheap_paid', 'paid'.
      limit: max rows (1-500, default 50).

    Returns the same shape as search_capabilities. With no filters,
    returns the whole registry (up to `limit`) ranked by intrinsic score.
    """
    conn = connect()
    try:
        return queries.browse_components(
            conn,
            component_kind=component_kind,
            ecosystem=ecosystem,
            runtime=runtime,
            cost_tier=cost_tier,
            project_id=project_id,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_constraint_fit(
    project_id: str,
    capability_id: str,
) -> Optional[dict[str, Any]]:
    """
    Full per-constraint verdict for one component against one project's
    project_constraint set.

    Args:
      project_id:    UUID of the project row.
      capability_id: UUID of the capability row.

    Returns {capability_id, normalized_key, display_name, hard_fail,
    verdicts:[{kind, passed, reason, detail}]} or None if either id
    is malformed or not found. If the project has no constraints,
    hard_fail is False and verdicts is [].
    """
    conn = connect()
    try:
        return queries.capability_constraint_fit(
            conn, project_id=project_id, capability_id=capability_id,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_compatibility(
    source_id: str,
    target_id: str,
) -> Optional[dict[str, Any]]:
    """
    Evaluate whether the source component can feed the target
    downstream. Reads runtimes and (when populated) the head-version
    interface I/O types.

    Args:
      source_id: UUID of the upstream/producing capability.
      target_id: UUID of the downstream/consuming capability.

    Returns {source, target, verdict, reason, detail, io_type_check,
    adapter_hint} or None on malformed / missing ids.

    verdict is one of:
      compatible      — same runtime family, I/O types match or absent
      adapter_needed  — different family with a known bridge, OR same
                        family with mismatched types
      incompatible    — no known bridge between the runtimes
    """
    conn = connect()
    try:
        return queries.capability_compatibility(
            conn, source_id=source_id, target_id=target_id,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_detail(capability_id: str) -> Optional[dict[str, Any]]:
    """
    Full record for one capability.

    Args:
      capability_id: UUID of the capability row.

    Returns {id, normalized_key, display_name, ecosystem, capability_kind,
    component_kind, runtime, cost_tier, license_spdx, first_seen_at,
    head_version, interfaces, dependencies, scorecard} or None if the id
    does not exist. Every interface and dependency carries an
    evidence_item_id so the caller can trace back to the byte range
    that established it.
    """
    conn = connect()
    try:
        return queries.capability_detail(conn, capability_id=capability_id)
    finally:
        conn.close()


@mcp.tool()
def suggest_pipeline(
    intent: str,
    project_id: Optional[str] = None,
    max_stages: int = 5,
) -> dict[str, Any]:
    """
    Decompose an intent into an ordered pipeline of stages and propose
    a component pick per stage from the CIP registry.

    Args:
      intent: plain-English description of what the pipeline must do.
      project_id: optional UUID — when set, candidate picks are
        pre-filtered by that project's project_constraint set.
      max_stages: cap on stages (default 5, hard max 12).

    Returns {intent, project_id, stages, edges, notes}:
      stages[]: {index, name, purpose, role, preferred_component_kind,
                 search_terms, candidates[], pick}
      edges[]:  adjacent-stage compatibility verdicts
      notes[]:  strings flagging gaps (no candidates in registry, etc.)

    The proposal is READ-ONLY — no rows are written. The human is the
    designer; this is a starting point to react to.
    """
    from scripts.compose.suggest_pipeline import (
        suggest_pipeline as _suggest, MAX_STAGES,
    )
    capped = max_stages if max_stages <= MAX_STAGES else MAX_STAGES
    proposal = _suggest(intent=intent, project_id=project_id, max_stages=capped)
    return {
        "intent": proposal.intent,
        "project_id": proposal.project_id,
        "stages": proposal.stages,
        "edges": proposal.edges,
        "notes": proposal.notes,
    }


@mcp.tool()
def scaffold_pipeline(
    proposal: dict[str, Any],
    out_dir: str,
    name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Turn a pipeline proposal (from suggest_pipeline, or hand-authored)
    into a real directory of files: pipeline.yaml, README.md, .env.example,
    and per-stage folders with README/TODO documentation. No LLM, no DB.

    Args:
      proposal: dict with keys {intent, project_id, stages[], edges[], notes[]}.
        Typically the return value of suggest_pipeline. Optional 'name' key
        may be embedded; overridden by the `name` arg if given.
      out_dir: directory to write into. Created if missing. Existing files
        are overwritten silently — meant for a fresh directory.
      name: optional override for the pipeline name. If neither `name` nor
        proposal['name'] is set, uses 'unnamed-pipeline'.

    Returns {out_dir, files_written[], env_vars_detected[]}.

    The scaffold is deliberately skeletal — it doesn't write runnable code.
    The wiring between stages is the operator's design call. Each stage
    folder has a TODO.md.
    """
    from pathlib import Path
    from scripts.compose.scaffold_pipeline import scaffold
    if name:
        proposal = {**proposal, "name": name}
    return scaffold(dict(proposal), Path(out_dir))


def main() -> None:
    """Entry point for the `cip-mcp` console script. Runs stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
