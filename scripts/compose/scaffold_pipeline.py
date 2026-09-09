"""
Pipeline scaffold generator (Phase 5c).

Takes the output of suggest_pipeline (or an equivalent hand-authored
proposal) and writes a real directory structure the operator can
open, wire, and run. No LLM. No DB. Pure I/O:

    <out_dir>/
      pipeline.yaml             ordered stage list + component ids
      README.md                 overview + how-to-run notes
      .env.example              env vars this pipeline's picks need
      stages/
        00_<name>/
          README.md             stage purpose + pick + alternatives +
                                adapter notes for edge INTO this stage
          TODO.md               explicit wiring TODOs
        01_<name>/
          ...

The scaffold is skeletal on purpose. It doesn't try to write runnable
Python for each stage — the picks change and the human is the designer.
What it DOES do: turn a chain proposal into a repo layout that
survives fork+modify.

Usage as a script:
    python -m scripts.compose.scaffold_pipeline \\
        --proposal-json proposal.json \\
        --out-dir ./my-new-pipeline

The proposal-json file is exactly the dict returned by suggest_pipeline
(or the MCP tool's return value written to disk).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml   # pyyaml is already a project dep


# Env vars the ingesters told us each runtime needs. Best-effort — we
# don't know every provider's key naming, but the common ones are here.
RUNTIME_ENV_HINTS = {
    "python_import":   [],
    "npm_import":      [],
    "mcp_stdio":       [],
    "mcp_sse":         ["MCP_SERVER_URL"],
    "mcp_http":        ["MCP_SERVER_URL"],
    "claude_skill":    [],
    "claude_agent":    [],
    "cli_subprocess":  [],
    "git_clone":       [],
    "http_endpoint":   ["API_BASE_URL", "API_KEY"],
}

METADATA_ENV_HINTS = {
    # If a component's normalized_key contains these tokens, add the
    # corresponding env var to the .env.example.
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "gemini":     "GEMINI_API_KEY",
    "sentry":     "SENTRY_DSN",
    "auth0":      "AUTH0_DOMAIN",
    "aws":        "AWS_ACCESS_KEY_ID",
    "boto":       "AWS_ACCESS_KEY_ID",
    "s3":         "AWS_ACCESS_KEY_ID",
    "github":     "GITHUB_TOKEN",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "runway":     "RUNWAY_API_KEY",
    "pika":       "PIKA_API_KEY",
    "tiktok":     "TIKTOK_ACCESS_TOKEN",
    "instagram":  "IG_ACCESS_TOKEN",
    "youtube":    "YOUTUBE_API_KEY",
}


_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    s = _SAFE_SLUG_RE.sub("-", text.lower()).strip("-")
    return s or "stage"


def _env_vars_for(proposal: dict) -> list[str]:
    """Best-effort list of env vars picks likely need."""
    hints: set[str] = set()
    for st in proposal.get("stages", []):
        pk = st.get("pick") or {}
        for tok, env in METADATA_ENV_HINTS.items():
            if tok in (pk.get("normalized_key") or "").lower():
                hints.add(env)
        for env in RUNTIME_ENV_HINTS.get(pk.get("runtime") or "", []):
            hints.add(env)
    return sorted(hints)


def _pipeline_yaml_dict(proposal: dict) -> dict:
    """The canonical pipeline.yaml shape."""
    return {
        "name":    proposal.get("name") or "unnamed-pipeline",
        "intent":  proposal.get("intent", ""),
        "project_id": proposal.get("project_id"),
        "stages": [
            {
                "index": st["index"],
                "name":  st["name"],
                "role":  st["role"],
                "purpose": st["purpose"],
                "component": (
                    {
                        "id":              st["pick"]["id"],
                        "normalized_key":  st["pick"]["normalized_key"],
                        "display_name":    st["pick"]["display_name"],
                        "runtime":         st["pick"].get("runtime"),
                    }
                    if st.get("pick") else None
                ),
                "alternatives": [
                    {"id": c["id"], "normalized_key": c["normalized_key"]}
                    for c in (st.get("candidates") or [])[1:3]
                ],
            }
            for st in proposal.get("stages", [])
        ],
        "edges": proposal.get("edges", []),
        "notes": proposal.get("notes", []),
    }


def _stage_readme(st: dict, incoming_edge: dict | None) -> str:
    lines = [
        f"# Stage {st['index']:02d} — {st['name']}",
        "",
        f"**Role:** {st['role']}",
        f"**Purpose:** {st['purpose']}",
        "",
    ]
    pk = st.get("pick")
    if pk:
        lines += [
            "## Pick",
            f"- **{pk['normalized_key']}** (`{pk['display_name']}`)",
            f"- Runtime: `{pk.get('runtime') or 'unknown'}`",
            f"- Component kind: `{pk.get('component_kind') or 'unknown'}`",
        ]
        if pk.get("cost_tier"):
            lines.append(f"- Cost tier: `{pk['cost_tier']}`")
        if pk.get("license_spdx"):
            lines.append(f"- License: `{pk['license_spdx']}`")
        if pk.get("total_score") is not None:
            lines.append(f"- Registry score: `{pk['total_score']}`")
    else:
        lines += [
            "## Pick",
            "**(none)** — the registry had no candidate for this stage's search terms.",
            "Fill this gap by either:",
            "- Ingesting more components (see `docs/ingestion_cadence.md`)",
            "- Writing your own; then adding a `project_constraint`-satisfying entry to the registry",
        ]

    alts = (st.get("candidates") or [])[1:4]
    if alts:
        lines += ["", "## Alternatives"]
        for a in alts:
            lines.append(f"- {a['normalized_key']}"
                         f" (score {a.get('total_score') or 'n/a'})")

    if incoming_edge:
        lines += ["", "## Edge from previous stage"]
        verdict = incoming_edge.get("verdict", "unknown")
        reason  = incoming_edge.get("reason", "")
        detail  = incoming_edge.get("detail", "")
        hint    = incoming_edge.get("adapter_hint", "")
        lines.append(f"- verdict: **{verdict}** ({reason})")
        if detail:
            lines.append(f"- detail:  {detail}")
        if hint:
            lines.append(f"- adapter hint: `{hint}`")

    lines += ["", "## Search terms used"]
    for t in st.get("search_terms", []):
        lines.append(f"- `{t}`")

    return "\n".join(lines) + "\n"


def _stage_todo(st: dict) -> str:
    return (
        f"# TODO for stage {st['index']:02d} — {st['name']}\n"
        f"\n"
        f"- [ ] Read this stage's README and confirm the pick is right for the intent.\n"
        f"- [ ] Wire the component into your pipeline runner.\n"
        f"- [ ] If an adapter is needed for the incoming edge, write the glue in\n"
        f"      an `adapter.py` (or equivalent for the runtime) in this folder.\n"
        f"- [ ] Add a smoke test that runs this stage on a fixed sample input.\n"
        f"- [ ] Document any env vars the pick needs.\n"
    )


def _root_readme(proposal: dict, env_vars: list[str]) -> str:
    lines = [
        f"# {proposal.get('name') or 'pipeline'}",
        "",
        "Scaffold generated by CIP `scaffold_pipeline`. Every stage is a folder",
        "under `stages/` with its picked component, alternatives, and adapter notes.",
        "",
        "## Intent",
        f"> {proposal.get('intent','(no intent recorded)')}",
        "",
        "## Stages",
    ]
    for st in proposal.get("stages", []):
        pick_key = (st.get("pick") or {}).get("normalized_key", "**(no pick)**")
        lines.append(f"- `{st['index']:02d}_{_slug(st['name'])}` — {st['name']} → {pick_key}")

    if env_vars:
        lines += ["", "## Env vars", "See `.env.example`. Set these before running any stage."]
    if proposal.get("notes"):
        lines += ["", "## Notes"]
        for n in proposal["notes"]:
            lines.append(f"- {n}")

    lines += [
        "",
        "## Not runnable yet",
        "This scaffold is deliberately skeletal. It does NOT include runnable code —",
        "the wiring between stages is the human's design call. Each stage folder has",
        "a `TODO.md` listing what to do next.",
        "",
    ]
    return "\n".join(lines)


def _env_example(env_vars: list[str]) -> str:
    if not env_vars:
        return "# No env vars auto-detected for this pipeline's picks.\n"
    lines = ["# Fill these before running any stage.",
             "# Do NOT commit real values.",
             ""]
    for e in env_vars:
        lines.append(f"{e}=")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scaffold(proposal: dict, out_dir: Path) -> dict[str, Any]:
    """
    Write the scaffold. Returns a small summary dict listing every
    file we wrote. Overwrites existing files silently — the operator
    is expected to be running this into a fresh directory.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    pipeline_yaml = _pipeline_yaml_dict(proposal)
    (out_dir / "pipeline.yaml").write_text(
        yaml.safe_dump(pipeline_yaml, sort_keys=False), encoding="utf-8",
    )
    written.append("pipeline.yaml")

    env_vars = _env_vars_for(proposal)
    (out_dir / ".env.example").write_text(_env_example(env_vars), encoding="utf-8")
    written.append(".env.example")

    (out_dir / "README.md").write_text(
        _root_readme(proposal, env_vars), encoding="utf-8",
    )
    written.append("README.md")

    stages_dir = out_dir / "stages"
    stages_dir.mkdir(exist_ok=True)

    edges_by_target: dict[int, dict] = {
        e["to_stage"]: e for e in proposal.get("edges", [])
    }

    for st in proposal.get("stages", []):
        folder = stages_dir / f"{st['index']:02d}_{_slug(st['name'])}"
        folder.mkdir(exist_ok=True)
        edge_in = edges_by_target.get(st["index"])
        (folder / "README.md").write_text(
            _stage_readme(st, edge_in), encoding="utf-8",
        )
        (folder / "TODO.md").write_text(_stage_todo(st), encoding="utf-8")
        written.append(str(folder.relative_to(out_dir) / "README.md"))
        written.append(str(folder.relative_to(out_dir) / "TODO.md"))

    return {"out_dir": str(out_dir), "files_written": written,
             "env_vars_detected": env_vars}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proposal-json", required=True,
                    help="Path to a JSON file with the suggest_pipeline output.")
    ap.add_argument("--out-dir", required=True,
                    help="Directory to write the scaffold into (created if missing).")
    args = ap.parse_args()
    proposal = json.loads(Path(args.proposal_json).read_text(encoding="utf-8"))
    result = scaffold(proposal, Path(args.out_dir))
    print(f"wrote {len(result['files_written'])} files to {result['out_dir']}")
    if result["env_vars_detected"]:
        print("env vars detected:", ", ".join(result["env_vars_detected"]))


if __name__ == "__main__":
    main()
