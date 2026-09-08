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


def main() -> None:
    """Entry point for the `cip-mcp` console script. Runs stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
