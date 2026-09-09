"""
Pure component-compatibility evaluator (Phase 5b).

Given two components (typically an upstream producer and a downstream
consumer), returns a verdict:

  compatible        — same runtime family, matching I/O types
  adapter_needed    — different runtime family, or type mismatch that
                       could be bridged (json over http, subprocess piping)
  incompatible      — no plausible bridge (missing runtime entirely,
                       incompatible sinks)

No DB, no LLM. Reads component_kind + runtime + capability_interface's
input_type/output_type (JSONB descriptors when populated) and applies
a static compatibility matrix.

I/O types are stored as JSONB in capability_interface.input_type /
.output_type. They're empty on most rows today; the evaluator
degrades gracefully — missing types don't guarantee compatibility,
they degrade the verdict to 'adapter_needed' with reason='unknown_io'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Runtime families that can call each other in-process without glue.
NATIVE_INTEROP = {
    "python_import":  {"python_import"},
    "npm_import":     {"npm_import"},
    "cargo_import":   {"cargo_import"},
    "go_import":      {"go_import"},
    "mcp_stdio":      {"mcp_stdio", "mcp_sse", "mcp_http"},
    "mcp_sse":        {"mcp_stdio", "mcp_sse", "mcp_http"},
    "mcp_http":       {"mcp_stdio", "mcp_sse", "mcp_http"},
    "claude_skill":   {"claude_skill", "claude_agent"},
    "claude_agent":   {"claude_skill", "claude_agent"},
    "cli_subprocess": {"cli_subprocess"},
    "git_clone":      {"git_clone"},
    "http_endpoint":  {"http_endpoint", "mcp_http", "mcp_sse"},
    "other":          set(),
}

# Runtime pairs that need an adapter but are bridgeable.
BRIDGEABLE = {
    ("python_import", "cli_subprocess"),
    ("cli_subprocess", "python_import"),
    ("python_import", "http_endpoint"),
    ("http_endpoint", "python_import"),
    ("npm_import",    "http_endpoint"),
    ("http_endpoint", "npm_import"),
    ("python_import", "mcp_stdio"),
    ("mcp_stdio",     "python_import"),
    ("cli_subprocess", "http_endpoint"),
    ("http_endpoint", "cli_subprocess"),
    ("git_clone",     "python_import"),
    ("git_clone",     "cli_subprocess"),
    ("claude_skill",  "mcp_stdio"),   # skill invokes a tool via MCP
    ("claude_agent",  "mcp_stdio"),
    ("claude_skill",  "python_import"),
    ("claude_agent",  "python_import"),
}


@dataclass
class CompatVerdict:
    verdict: str            # 'compatible' | 'adapter_needed' | 'incompatible'
    reason: str             # short slug
    detail: str = ""        # human-readable extra
    io_type_check: str = "not_evaluated"   # 'match' | 'mismatch' | 'unknown_io' | 'not_evaluated'
    adapter_hint: str = ""  # suggestion text if adapter_needed


# ---------------------------------------------------------------------------
# Runtime interop
# ---------------------------------------------------------------------------

def _runtime_verdict(src_rt: str, tgt_rt: str) -> tuple[str, str, str]:
    """Returns (verdict, reason, hint)."""
    if not src_rt or not tgt_rt:
        return ("adapter_needed", "unknown_runtime",
                "one side has no declared runtime; assume glue")
    src_family = NATIVE_INTEROP.get(src_rt, set())
    if tgt_rt in src_family:
        return ("compatible", "same_family", "")
    pair = (src_rt, tgt_rt)
    if pair in BRIDGEABLE:
        return ("adapter_needed", "bridge_available",
                _bridge_hint(src_rt, tgt_rt))
    return ("incompatible", "no_known_bridge",
            f"no adapter pattern between {src_rt} and {tgt_rt}")


def _bridge_hint(src: str, tgt: str) -> str:
    hints = {
        ("python_import", "cli_subprocess"):  "subprocess.run() call, capture stdout",
        ("cli_subprocess", "python_import"):  "subprocess.run() call, capture stdout",
        ("python_import", "http_endpoint"):   "httpx.Client call",
        ("http_endpoint", "python_import"):   "httpx.Client call",
        ("npm_import",    "http_endpoint"):   "fetch() call",
        ("http_endpoint", "npm_import"):      "fetch() call",
        ("python_import", "mcp_stdio"):       "mcp.ClientSession + stdio_client",
        ("mcp_stdio",     "python_import"):   "mcp.ClientSession + stdio_client",
        ("git_clone",     "python_import"):   "pip install -e . after clone",
        ("git_clone",     "cli_subprocess"):  "clone then run the CLI binary from the tree",
        ("claude_skill",  "mcp_stdio"):       "skill invokes the MCP tool by name",
        ("claude_agent",  "mcp_stdio"):       "agent calls the tool via its MCP client",
    }
    return hints.get((src, tgt), "generic glue script")


# ---------------------------------------------------------------------------
# I/O type check
# ---------------------------------------------------------------------------

def _io_types_match(src_output: dict | None, tgt_input: dict | None) -> str:
    """
    Returns 'match' | 'mismatch' | 'unknown_io'.
    A permissive comparator — same 'kind' field is a match, missing kinds
    are unknown, differing kinds are mismatch.
    """
    if not src_output and not tgt_input:
        return "unknown_io"
    if not src_output or not tgt_input:
        return "unknown_io"
    s_kind = (src_output.get("kind") or "").lower()
    t_kind = (tgt_input.get("kind") or "").lower()
    if not s_kind or not t_kind:
        return "unknown_io"
    if s_kind != t_kind:
        return "mismatch"
    # Same kind. Look at the 'of' or 'type' for a deeper check when present.
    for f in ("type", "of"):
        sv = str(src_output.get(f, "")).lower()
        tv = str(tgt_input.get(f, "")).lower()
        if sv and tv and sv != tv:
            return "mismatch"
    return "match"


# ---------------------------------------------------------------------------
# Public: check_pair
# ---------------------------------------------------------------------------

def check_pair(
    source: dict[str, Any],
    target: dict[str, Any],
    source_output_type: dict | None = None,
    target_input_type: dict | None = None,
) -> CompatVerdict:
    """
    Evaluate whether `source` can feed `target`.

    source / target dicts need at minimum:  {runtime: str}
    source_output_type / target_input_type are the JSONB payloads from
    capability_interface — pass None when unknown.
    """
    src_rt = (source.get("runtime") or "").lower()
    tgt_rt = (target.get("runtime") or "").lower()
    verdict, reason, hint = _runtime_verdict(src_rt, tgt_rt)

    io_status = _io_types_match(source_output_type, target_input_type)

    # If runtime says compatible but I/O types mismatch outright, downgrade.
    if verdict == "compatible" and io_status == "mismatch":
        return CompatVerdict(
            verdict="adapter_needed", reason="io_mismatch_within_family",
            detail=f"{src_rt} <-> {tgt_rt} runtime-compatible but I/O types differ",
            io_type_check=io_status,
            adapter_hint="write a type transformer",
        )
    # If runtime bridgeable and I/O matches, keep adapter_needed but flag good.
    return CompatVerdict(
        verdict=verdict, reason=reason,
        detail=f"{src_rt} -> {tgt_rt}",
        io_type_check=io_status,
        adapter_hint=hint if verdict == "adapter_needed" else "",
    )
