"""
MCP registry ingester.

Reads MCP servers the user has configured in ~/.claude.json (across all
project scopes plus root scope), and writes each unique server as a
component_kind='mcp_tool' capability. Metadata captures the invocation
(command + args, or url) and which project scopes it appears in — but
NEVER the env values (those may hold secrets).

This gives CIP a live view of "what MCP servers is this user actually
running?" — the highest-signal MCP data available, since these are
servers the user has already vetted.

Usage:
    python -m scripts.ingest.mcp_registry
    python -m scripts.ingest.mcp_registry --claude-json ~/other-config.json

Idempotent. Safe to cron. No network calls — reads local JSON only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from db.connection import connect
from scripts.ingest import _base

SOURCE_NAME = "claude_mcp_registrations"


@dataclass
class ServerConfig:
    name: str
    transport: str                    # 'stdio' | 'sse' | 'http' | 'unknown'
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    url: Optional[str] = None
    env_keys: list[str] = field(default_factory=list)   # names only, never values
    scopes: list[str] = field(default_factory=list)     # project paths, or 'user'

    def runtime(self) -> str:
        return {
            "stdio": "mcp_stdio",
            "sse":   "mcp_sse",
            "http":  "mcp_http",
        }.get(self.transport, "mcp_stdio")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _classify_transport(cfg: dict) -> str:
    """Heuristic — the JSON schema for MCP config varies by client version."""
    t = (cfg.get("type") or "").lower()
    if t in ("stdio", "sse", "http"):
        return t
    if cfg.get("url"):
        return "sse"
    if cfg.get("command"):
        return "stdio"
    return "unknown"


def _server_from_entry(name: str, cfg: dict, scope: str) -> ServerConfig:
    return ServerConfig(
        name=name,
        transport=_classify_transport(cfg),
        command=cfg.get("command"),
        args=list(cfg.get("args") or []),
        url=cfg.get("url"),
        env_keys=list((cfg.get("env") or {}).keys()),
        scopes=[scope],
    )


def _walk_mcp_sections(root: dict) -> Iterable[tuple[str, dict, str]]:
    """Yield (name, config, scope) for every mcpServers section found.
    Scopes are project paths, or 'user' for root-level."""
    root_mcp = root.get("mcpServers") or {}
    for name, cfg in root_mcp.items():
        yield (name, cfg, "user")
    projects = root.get("projects") or {}
    for project_path, project_data in projects.items():
        if not isinstance(project_data, dict):
            continue
        for name, cfg in (project_data.get("mcpServers") or {}).items():
            yield (name, cfg, project_path)


def parse_claude_json(path: Path) -> list[ServerConfig]:
    """Read the config, dedupe servers by name, merge scopes."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))

    by_name: dict[str, ServerConfig] = {}
    for name, cfg, scope in _walk_mcp_sections(data):
        if not isinstance(cfg, dict):
            continue
        parsed = _server_from_entry(name, cfg, scope)
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = parsed
            continue
        # Same-named entries across projects — merge scope, keep first
        # transport/command/args (all instances should match; if they
        # don't we log which ones differ and keep the first).
        if scope not in existing.scopes:
            existing.scopes.append(scope)
        if (parsed.command, parsed.args, parsed.url, parsed.transport) != (
            existing.command, existing.args, existing.url, existing.transport
        ):
            existing.scopes.append(f"{scope}#config-differs")
    return list(by_name.values())


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_one(conn, s: ServerConfig) -> str:
    metadata = {
        "transport": s.transport,
        "command": s.command,
        "args": s.args,
        "url": s.url,
        "env_keys": s.env_keys,          # names only, never values
        "scopes": s.scopes,
    }
    normalized_key = f"mcp:{s.name}"
    _cap_id, was_new, was_updated = _base.upsert_component(
        conn,
        normalized_key=normalized_key,
        display_name=s.name,
        ecosystem="source",
        capability_kind="service",
        component_kind="mcp_tool",
        runtime=s.runtime(),
        cost_tier="free",   # a locally-registered server is free at the wire; the service it fronts may charge
        license_spdx=None,
        metadata=metadata,
    )
    if was_new:
        return "new"
    if was_updated:
        return "updated"
    return "unchanged"


def run_ingest(claude_json_path: Optional[Path] = None) -> dict[str, Any]:
    path = claude_json_path or Path(os.path.expanduser("~/.claude.json"))
    servers = parse_claude_json(path)
    print(f"({len(servers)} unique MCP server(s) found in {path.name})")

    conn = connect()
    try:
        metadata = {"claude_json_path": str(path), "server_count": len(servers)}
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            for s in servers:
                try:
                    outcome = ingest_one(conn, s)
                    if outcome == "new":         counts.new += 1
                    elif outcome == "updated":     counts.updated += 1
                    elif outcome == "unchanged":   counts.unchanged += 1
                    else:                            counts.errors += 1
                    print(f"  {outcome:10s}  mcp:{s.name:30s} transport={s.transport}")
                    conn.commit()
                except Exception as e:
                    counts.errors += 1
                    conn.rollback()
                    print(f"  error       mcp:{s.name}: {e}", file=sys.stderr)
            metadata["cursor"] = None   # local-file source has no time cursor
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-json", default=None,
                    help="Override the config path (default: ~/.claude.json).")
    args = ap.parse_args()
    path = Path(os.path.expanduser(args.claude_json)) if args.claude_json else None
    run_ingest(claude_json_path=path)


if __name__ == "__main__":
    main()
