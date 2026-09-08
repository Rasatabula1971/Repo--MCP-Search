"""
Phase 2 done-when for the MCP registry ingester:

  "parse_claude_json extracts every unique MCP server from all
   project scopes and the root scope, merges duplicate names across
   projects, classifies the transport, and never captures env values."

Test coverage is on the pure parser + ingest_one against a real DB.
No filesystem walks in tests — all input is inline JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ingest import mcp_registry


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _write(tmp: Path, data: dict) -> Path:
    p = tmp / "claude.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_parse_returns_empty_when_file_missing(tmp_path):
    assert mcp_registry.parse_claude_json(tmp_path / "does-not-exist.json") == []


def test_parse_returns_empty_when_no_mcpservers(tmp_path):
    p = _write(tmp_path, {"projects": {"/foo": {}}})
    assert mcp_registry.parse_claude_json(p) == []


def test_parse_captures_project_scoped_stdio_server(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {
                "mcpServers": {
                    "cip": {"type": "stdio", "command": "cip-mcp", "args": []}
                }
            }
        }
    })
    servers = mcp_registry.parse_claude_json(p)
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "cip"
    assert s.transport == "stdio"
    assert s.command == "cip-mcp"
    assert s.scopes == ["/proj/A"]


def test_parse_captures_sse_server_with_url(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {
                "mcpServers": {
                    "gdrive": {"type": "sse", "url": "https://example.com/mcp"}
                }
            }
        }
    })
    servers = mcp_registry.parse_claude_json(p)
    assert servers[0].transport == "sse"
    assert servers[0].url == "https://example.com/mcp"
    assert servers[0].runtime() == "mcp_sse"


def test_parse_infers_stdio_when_type_missing_but_command_present(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {
                "mcpServers": {
                    "x": {"command": "run-x"}
                }
            }
        }
    })
    assert mcp_registry.parse_claude_json(p)[0].transport == "stdio"


def test_parse_infers_sse_when_url_present_and_type_missing(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {
                "mcpServers": {
                    "y": {"url": "https://example.com"}
                }
            }
        }
    })
    assert mcp_registry.parse_claude_json(p)[0].transport == "sse"


def test_parse_merges_same_named_server_across_projects(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {"mcpServers": {"cip": {"type": "stdio", "command": "cip-mcp"}}},
            "/proj/B": {"mcpServers": {"cip": {"type": "stdio", "command": "cip-mcp"}}},
            "/proj/C": {"mcpServers": {"cip": {"type": "stdio", "command": "cip-mcp"}}},
        }
    })
    servers = mcp_registry.parse_claude_json(p)
    assert len(servers) == 1
    assert sorted(servers[0].scopes) == ["/proj/A", "/proj/B", "/proj/C"]


def test_parse_flags_scope_when_configs_diverge(tmp_path):
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {"mcpServers": {"x": {"type": "stdio", "command": "run-x-v1"}}},
            "/proj/B": {"mcpServers": {"x": {"type": "stdio", "command": "run-x-v2"}}},
        }
    })
    servers = mcp_registry.parse_claude_json(p)
    assert len(servers) == 1
    # One scope tagged as differing.
    assert any("config-differs" in s for s in servers[0].scopes)


def test_parse_captures_root_level_mcpservers_as_user_scope(tmp_path):
    p = _write(tmp_path, {
        "mcpServers": {"global-thing": {"type": "stdio", "command": "gt"}}
    })
    servers = mcp_registry.parse_claude_json(p)
    assert servers[0].scopes == ["user"]


def test_parse_records_env_key_names_but_not_values(tmp_path):
    """The .env values are the whole point of MCP env config; they're
    also the whole point of NOT persisting them in the registry."""
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": {
                "mcpServers": {
                    "authy": {
                        "type": "stdio",
                        "command": "authy-mcp",
                        "env": {"API_KEY": "super-secret-value", "DEBUG": "1"},
                    }
                }
            }
        }
    })
    s = mcp_registry.parse_claude_json(p)[0]
    assert sorted(s.env_keys) == ["API_KEY", "DEBUG"]
    # And nothing anywhere in the parsed object holds the secret value.
    serialized = json.dumps({
        "name": s.name, "command": s.command, "args": s.args,
        "url": s.url, "env_keys": s.env_keys, "scopes": s.scopes,
    })
    assert "super-secret-value" not in serialized


def test_parse_skips_non_dict_project_entries(tmp_path):
    """Malformed configs shouldn't crash parsing."""
    p = _write(tmp_path, {
        "projects": {
            "/proj/A": "not-a-dict",
            "/proj/B": {"mcpServers": {"ok": {"type": "stdio", "command": "ok"}}},
        }
    })
    servers = mcp_registry.parse_claude_json(p)
    assert len(servers) == 1
    assert servers[0].name == "ok"


# ---------------------------------------------------------------------------
# ingest_one
# ---------------------------------------------------------------------------

def test_ingest_one_writes_mcp_tool_capability(conn):
    s = mcp_registry.ServerConfig(
        name="cip", transport="stdio",
        command="cip-mcp", args=[],
        env_keys=["GITHUB_TOKEN"], scopes=["/proj/A"],
    )
    outcome = mcp_registry.ingest_one(conn, s)
    conn.commit()
    assert outcome == "new"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, component_kind, runtime, cost_tier, metadata "
            "FROM capability WHERE normalized_key = 'mcp:cip'"
        )
        row = cur.fetchone()
    assert row[0] == "cip"
    assert row[1] == "mcp_tool"
    assert row[2] == "mcp_stdio"
    assert row[3] == "free"
    assert row[4]["command"] == "cip-mcp"
    assert row[4]["env_keys"] == ["GITHUB_TOKEN"]


def test_ingest_one_is_idempotent(conn):
    s = mcp_registry.ServerConfig(name="cip", transport="stdio",
                                    command="cip-mcp", scopes=["/proj/A"])
    mcp_registry.ingest_one(conn, s)
    conn.commit()
    assert mcp_registry.ingest_one(conn, s) == "unchanged"


def test_ingest_one_detects_scope_change(conn):
    s1 = mcp_registry.ServerConfig(name="cip", transport="stdio",
                                     command="cip-mcp", scopes=["/proj/A"])
    mcp_registry.ingest_one(conn, s1)
    conn.commit()

    s2 = mcp_registry.ServerConfig(name="cip", transport="stdio",
                                     command="cip-mcp",
                                     scopes=["/proj/A", "/proj/B"])
    assert mcp_registry.ingest_one(conn, s2) == "updated"
