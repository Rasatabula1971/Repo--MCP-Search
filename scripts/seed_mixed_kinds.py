"""
Seed one row per component_kind so Phase 1 is provable end-to-end.

Not a real catalog — a smoke seed. Each entry is a real, identifiable
thing the user could look up; the point is that browse_components(kind=X)
returns something for every X, and capability_detail on each returns the
new metadata fields correctly.

Idempotent — safe to rerun.

Run:
    python -m scripts.seed_mixed_kinds
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from db.connection import connect


@dataclass
class Seed:
    normalized_key: str
    display_name: str
    ecosystem: str
    capability_kind: str
    component_kind: str
    runtime: str | None
    cost_tier: str | None
    license_spdx: str | None
    note: str


SEEDS: list[Seed] = [
    # library — sanity-check that the existing kind still ingests via
    # this seeder too. (The demo seeder covers the twelve real libraries.)
    Seed(
        normalized_key="pypi:example-library-marker",
        display_name="example-library-marker",
        ecosystem="pypi",
        capability_kind="library",
        component_kind="library",
        runtime="python_import",
        cost_tier="free",
        license_spdx="MIT",
        note="Placeholder to prove library kind stores end-to-end.",
    ),
    # repo — a real awesome-list is a good example: whole GitHub project,
    # not a published package, but valuable as a discovery source.
    Seed(
        normalized_key="source:github:ad-si/awesome-video-production",
        display_name="ad-si/awesome-video-production",
        ecosystem="source",
        capability_kind="library",     # semantic role: still a "list of things"
        component_kind="repo",
        runtime="git_clone",
        cost_tier="free",
        license_spdx="CC0-1.0",
        note="Curated list of video-production tools — index into other components.",
    ),
    # mcp_tool — self-referential: the CIP MCP server itself.
    Seed(
        normalized_key="mcp:cip",
        display_name="cip",
        ecosystem="source",
        capability_kind="service",
        component_kind="mcp_tool",
        runtime="mcp_stdio",
        cost_tier="free",
        license_spdx="MIT",
        note="This registry's own MCP server. Recursive dogfood.",
    ),
    # skill — one of the Anthropic-published Claude skills.
    Seed(
        normalized_key="skill:anthropic/code-review",
        display_name="anthropic-skills:code-review",
        ecosystem="source",
        capability_kind="service",
        component_kind="skill",
        runtime="claude_skill",
        cost_tier="free",
        license_spdx=None,
        note="Reviews a diff or PR for correctness and cleanups.",
    ),
    # agent — a Claude Code built-in agent.
    Seed(
        normalized_key="agent:claude-code/explore",
        display_name="claude-code:Explore",
        ecosystem="source",
        capability_kind="service",
        component_kind="agent",
        runtime="claude_agent",
        cost_tier="free",
        license_spdx=None,
        note="Read-only search agent for locating code across a repo.",
    ),
    # workflow_template — placeholder, to be filled by Phase 5 (composition).
    Seed(
        normalized_key="workflow:cip/placeholder-demo",
        display_name="placeholder-demo",
        ecosystem="source",
        capability_kind="service",
        component_kind="workflow_template",
        runtime="other",
        cost_tier="free",
        license_spdx="MIT",
        note="Empty placeholder — Phase 5 will define real workflow_template shape.",
    ),
]


def seed_one(cur, s: Seed) -> str:
    cur.execute(
        "INSERT INTO capability "
        "(normalized_key, display_name, ecosystem, kind, "
        " component_kind, runtime, cost_tier, license_spdx, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (normalized_key) DO UPDATE SET "
        "  display_name   = EXCLUDED.display_name, "
        "  component_kind = EXCLUDED.component_kind, "
        "  runtime        = EXCLUDED.runtime, "
        "  cost_tier      = EXCLUDED.cost_tier, "
        "  license_spdx   = EXCLUDED.license_spdx, "
        "  metadata       = EXCLUDED.metadata "
        "RETURNING xmax = 0",  # true when inserted, false when updated
        (s.normalized_key, s.display_name, s.ecosystem, s.capability_kind,
         s.component_kind, s.runtime, s.cost_tier, s.license_spdx,
         json.dumps({"seed_note": s.note})),
    )
    inserted = cur.fetchone()[0]
    return f"{'seed' if inserted else 'update'}  {s.normalized_key:52s}  kind={s.component_kind}"


def main() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            for s in SEEDS:
                print(seed_one(cur, s))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
