"""
Pipeline suggester (Phase 5b).

Given an intent ("build a video production pipeline that ingests raw
footage, generates transcripts, and produces subtitled MP4s"), returns
a proposed chain:
  1. Gemini decomposes the intent into 3-8 stages (name + purpose +
     search terms).
  2. For each stage, browse_components + search_capabilities against
     the registry, filtered by project_id constraints if given.
  3. Rank stage candidates by component_score.total_score
     (score-per-kind from Phase 3d).
  4. Assemble the chain, run capability_compatibility between adjacent
     picks, annotate each edge.
  5. Return the proposal — never writes to workflow_template. That's a
     separate act; the model may hallucinate and the human is the
     designer.

The output is a Python dict that fits in a Claude reply and can be
handed to Phase 5c's scaffold_pipeline verbatim.

Usage as a script:
    python -m scripts.compose.suggest_pipeline \\
        --intent "faceless short-form AI video pipeline for social" \\
        --project-id <uuid>

    python -m scripts.compose.suggest_pipeline \\
        --intent "control-plane for commissioning external video jobs" \\
        --max-stages 6

Usage as a library — the function suggest_pipeline() is what the MCP
tool wraps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from db.connection import connect
from mcp_server import queries as q
from scripts.ingest import _base

load_dotenv()

SOURCE_NAME     = "suggest_pipeline"
GEMINI_MODEL    = "gemini-3.5-flash-lite"
GEMINI_URL      = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)
DEFAULT_STAGES  = 5
MAX_STAGES      = 12
CANDIDATES_PER_STAGE = 3

PROMPT_NAME     = "pipeline_decomposition"
PROMPT_VERSION  = 1


class GeminiError(Exception):
    pass


class MalformedResponse(Exception):
    pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You decompose a technical intent into an ordered pipeline of "
    "stages. Each stage names one job the pipeline must do. Respond "
    "with STRICT JSON. No prose, no markdown fences."
)


def _render_prompt(intent: str, max_stages: int,
                    project_constraint_summary: Optional[str] = None) -> str:
    body = {
        "task": "Decompose this intent into an ordered pipeline of stages",
        "intent": intent,
        "max_stages": max_stages,
        "response_schema": {
            "stages": [
                {
                    "name":       "short kebab-case slug (e.g. 'transcribe-audio')",
                    "purpose":    "one plain sentence describing what this stage does",
                    "search_terms": [
                        "3-6 short keywords to look up in the component registry"
                    ],
                    "role":       "producer | transformer | sink",
                    "preferred_component_kind": (
                        "library | repo | mcp_tool | skill | agent — "
                        "which KIND of component best fits this stage"
                    ),
                }
            ]
        },
        "rules": [
            "Return ONLY the JSON object.",
            "3 to " + str(max_stages) + " stages. Fewer if the intent is narrow.",
            "Stages are ordered — earlier stages produce data later stages consume.",
            "Each stage does ONE thing. If it needs decomposition, split it.",
            "search_terms are what a keyword search over library / repo / skill "
            "names would match against — think 'ffmpeg', 'whisper', 'openai', "
            "not full sentences.",
            "Prefer preferred_component_kind='library' for well-known primitives, "
            "'mcp_tool' for LLM-callable services, 'repo' when a whole project "
            "is the fit (e.g. an editing UI), 'skill' for LLM instruction sets.",
        ],
    }
    if project_constraint_summary:
        body["project_constraints"] = project_constraint_summary
    return json.dumps(body, indent=2)


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def call_gemini(prompt_text: str, api_key: str,
                client: Optional[httpx.Client] = None) -> str:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1600,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }
    own = client is None
    if own:
        client = httpx.Client(timeout=60.0)
    try:
        r = client.post(GEMINI_URL, params={"key": api_key}, json=body)
        if r.status_code == 429:
            raise GeminiError(f"rate_limited: {r.text[:200]}")
        if r.status_code >= 400:
            raise GeminiError(f"http_{r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            raise GeminiError(f"unexpected_response: {json.dumps(data)[:200]}")
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

VALID_ROLES = {"producer", "transformer", "sink"}
VALID_KINDS = {"library", "repo", "mcp_tool", "skill", "agent",
               "workflow_template"}


@dataclass
class ProposedStage:
    name: str
    purpose: str
    search_terms: list[str]
    role: str
    preferred_component_kind: str


def parse_stages(raw: str) -> list[ProposedStage]:
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise MalformedResponse(f"not_json: {e}")
    if not isinstance(obj, dict) or "stages" not in obj:
        raise MalformedResponse("missing_stages_field")
    stages_raw = obj["stages"]
    if not isinstance(stages_raw, list) or not stages_raw:
        raise MalformedResponse("stages_not_a_nonempty_list")
    stages: list[ProposedStage] = []
    for i, s in enumerate(stages_raw):
        if not isinstance(s, dict):
            raise MalformedResponse(f"stage_{i}_not_object")
        for f in ("name", "purpose", "search_terms", "role",
                  "preferred_component_kind"):
            if f not in s:
                raise MalformedResponse(f"stage_{i}_missing_{f}")
        role = str(s["role"]).lower().strip()
        if role not in VALID_ROLES:
            raise MalformedResponse(f"stage_{i}_bad_role: {role!r}")
        kind = str(s["preferred_component_kind"]).lower().strip()
        if kind not in VALID_KINDS:
            raise MalformedResponse(f"stage_{i}_bad_kind: {kind!r}")
        terms = s["search_terms"]
        if not isinstance(terms, list) or not terms:
            raise MalformedResponse(f"stage_{i}_bad_search_terms")
        stages.append(ProposedStage(
            name=str(s["name"]).strip()[:60],
            purpose=str(s["purpose"]).strip()[:280],
            search_terms=[str(t).strip() for t in terms if str(t).strip()][:8],
            role=role, preferred_component_kind=kind,
        ))
    return stages


# ---------------------------------------------------------------------------
# Candidate finding per stage
# ---------------------------------------------------------------------------

def find_candidates_for_stage(
    conn, stage: ProposedStage, project_id: Optional[str],
    per_stage: int = CANDIDATES_PER_STAGE,
) -> list[dict[str, Any]]:
    """
    Union of search_capabilities() results across the stage's terms,
    dedupe by capability id, keep the top per_stage by intrinsic
    total_score. Prefer the stage's preferred_component_kind but don't
    hard-filter — a stage that asked for 'library' may still want
    a 'repo' if that's the actual answer.
    """
    hits: dict[str, dict[str, Any]] = {}
    for term in stage.search_terms:
        for row in q.search_capabilities(
            conn, query=term,
            component_kind=stage.preferred_component_kind,
            project_id=project_id, limit=per_stage,
        ):
            hits.setdefault(row["id"], row)
        # Widen when the kind-filter came back empty for this term.
        if not any(r for r in hits.values()):
            for row in q.search_capabilities(
                conn, query=term, project_id=project_id, limit=per_stage,
            ):
                hits.setdefault(row["id"], row)

    ranked = sorted(
        hits.values(),
        key=lambda r: (r.get("total_score") or 0.0),
        reverse=True,
    )
    return ranked[:per_stage]


# ---------------------------------------------------------------------------
# Chain assembly + compatibility annotation
# ---------------------------------------------------------------------------

def _load_project_constraint_summary(conn, project_id: Optional[str]) -> Optional[str]:
    if not project_id:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, detail FROM project_constraint WHERE project_id::text = %s",
            (project_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    return "; ".join(f"{k}={json.dumps(d)}" for k, d in rows)


@dataclass
class SuggestedPipeline:
    intent: str
    project_id: Optional[str]
    stages: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _adjacent_compat(conn, a_id: str, b_id: str) -> dict[str, Any]:
    return q.capability_compatibility(conn, a_id, b_id) or {
        "verdict": "unknown", "reason": "unresolved"
    }


def suggest_pipeline(
    intent: str,
    project_id: Optional[str] = None,
    max_stages: int = DEFAULT_STAGES,
    conn=None,
) -> SuggestedPipeline:
    """
    conn: optional psycopg connection. When None (production use), we
    open one against DATABASE_URL. Tests pass the fixture conn (which
    points at TEST_DATABASE_URL) to avoid reading from the main DB.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set.")

    own_conn = conn is None
    if own_conn:
        conn = connect()
    metadata = {"intent": intent[:200], "project_id": project_id,
                "max_stages": max_stages}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            constraint_summary = _load_project_constraint_summary(conn, project_id)
            prompt = _render_prompt(intent, max_stages, constraint_summary)
            client = httpx.Client(timeout=60.0)
            try:
                raw = call_gemini(prompt, api_key, client=client)
            finally:
                client.close()

            stages = parse_stages(raw)
            counts.new = len(stages)

            proposal = SuggestedPipeline(intent=intent, project_id=project_id)
            for i, s in enumerate(stages):
                candidates = find_candidates_for_stage(conn, s, project_id)
                proposal.stages.append({
                    "index": i,
                    "name": s.name,
                    "purpose": s.purpose,
                    "role": s.role,
                    "preferred_component_kind": s.preferred_component_kind,
                    "search_terms": s.search_terms,
                    "candidates": candidates,   # top-N, ranked
                    "pick": candidates[0] if candidates else None,
                })
                if not candidates:
                    proposal.notes.append(
                        f"stage {i} '{s.name}': NO candidates in registry — "
                        f"gap to fill or ingest more"
                    )

            # Edges: adjacent-stage compatibility on the top pick per stage.
            for i in range(len(proposal.stages) - 1):
                a = proposal.stages[i]["pick"]
                b = proposal.stages[i + 1]["pick"]
                if not a or not b:
                    proposal.edges.append({
                        "from_stage": i, "to_stage": i + 1,
                        "verdict": "unresolved", "reason": "missing_pick",
                    })
                    continue
                edge = _adjacent_compat(conn, a["id"], b["id"])
                proposal.edges.append({
                    "from_stage": i, "to_stage": i + 1,
                    **edge,
                })

            return proposal
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_proposal(p: SuggestedPipeline) -> None:
    print(f"\n=== Pipeline proposal ===")
    print(f"intent:  {p.intent}")
    if p.project_id:
        print(f"project: {p.project_id}")
    print()
    for st in p.stages:
        head = f"  [{st['index']}] {st['name']:25s}  ({st['role']}, prefer {st['preferred_component_kind']})"
        print(head)
        print(f"      purpose: {st['purpose']}")
        if st["pick"]:
            pk = st["pick"]
            score = pk.get("total_score")
            score_s = f"{score:.2f}" if score is not None else "-"
            print(f"      pick:    {pk['normalized_key']:60s}  score={score_s}")
        else:
            print(f"      pick:    (none)")
        alts = st["candidates"][1:]
        for alt in alts[:2]:
            print(f"      alt:     {alt['normalized_key']}  score="
                  f"{(alt.get('total_score') or 0):.2f}")
    if p.edges:
        print("\n  edges:")
        for e in p.edges:
            print(f"    {e['from_stage']}->{e['to_stage']:2d}  "
                  f"{e.get('verdict','?'):16s}  {e.get('reason','')}")
    if p.notes:
        print("\n  notes:")
        for n in p.notes:
            print(f"    - {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--intent", required=True)
    ap.add_argument("--project-id", default=None)
    ap.add_argument("--max-stages", type=int, default=DEFAULT_STAGES)
    args = ap.parse_args()
    if args.max_stages > MAX_STAGES:
        args.max_stages = MAX_STAGES
    proposal = suggest_pipeline(intent=args.intent,
                                 project_id=args.project_id,
                                 max_stages=args.max_stages)
    _print_proposal(proposal)


if __name__ == "__main__":
    main()
