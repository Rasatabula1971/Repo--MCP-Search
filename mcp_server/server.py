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
    kind: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Search the CIP capability registry.

    Args:
      query: case-insensitive substring against display_name and normalized_key.
      ecosystem: optional filter — e.g. "pypi", "npm", "source".
      kind: optional filter — e.g. "library", "cli", "service".
      limit: max rows to return (1-100, default 20).

    Returns a list of {id, normalized_key, display_name, ecosystem, kind,
    head_version_id, display_version, total_score, confidence}. Rows are
    ordered by intrinsic score descending; unscored capabilities come
    last.
    """
    conn = connect()
    try:
        return queries.search_capabilities(
            conn, query=query, ecosystem=ecosystem, kind=kind, limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def capability_detail(capability_id: str) -> Optional[dict[str, Any]]:
    """
    Full record for one capability.

    Args:
      capability_id: UUID of the capability row.

    Returns {id, normalized_key, display_name, ecosystem, kind,
    first_seen_at, head_version, interfaces, dependencies, scorecard} or
    None if the id does not exist. Every interface and dependency carries
    an evidence_item_id so the caller can trace back to the byte range
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
