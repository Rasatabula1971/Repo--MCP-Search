"""
Component classifier (Phase 3a).

For each capability that hasn't been classified at the current prompt
version, sends a compact JSON prompt to Gemini asking:

    - What KIND of component is this? (library | repo | agent | skill |
      mcp_tool | workflow_template)
    - What ROLE does it play? (library | cli | service | framework |
      tool | dataset | doc | template)
    - What does it do? (one sentence, under 20 words)
    - How CONFIDENT are you? (0.00 - 1.00)
    - Why? (one sentence)

Records the answer in `component_classification` (append-only). If
confidence >= APPLY_THRESHOLD and the model disagrees with the current
row, we also mutate capability.component_kind / .capability_kind. The
prior values are snapshotted in the classification row for audit.

Reuses the ingest_run log for run-level tracking (source_name=
'classify_components'), so the same tools that watch ingestion see
this too.

Cadence-safe: rerunning at the same prompt version skips already-
classified rows. Bumping PROMPT_VERSION re-classifies everything.

Usage:
    python -m scripts.judge.classify_components --max 30
    python -m scripts.judge.classify_components --max 5 --dry-run
    python -m scripts.judge.classify_components --kind repo --max 50
    python -m scripts.judge.classify_components --force  # ignore already-classified
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from db.connection import connect
from scripts.ingest import _base

load_dotenv()

SOURCE_NAME       = "classify_components"
GEMINI_MODEL      = "gemini-3.5-flash-lite"
GEMINI_URL        = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)
MIN_INTERVAL_SEC  = 4.0
APPLY_THRESHOLD   = 0.80

PROMPT_NAME       = "component_classification"
PROMPT_VERSION    = 1

VALID_COMPONENT_KINDS  = {"library", "repo", "agent", "skill",
                          "mcp_tool", "workflow_template"}
VALID_CAPABILITY_KINDS = {"library", "cli", "service", "framework",
                          "tool", "dataset", "doc", "template"}


class GeminiError(Exception):
    pass


class MalformedResponse(Exception):
    pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You classify software components for a technical registry. "
    "Respond with STRICT JSON matching the schema. No prose, no "
    "markdown fences, just the JSON object."
)


def _render_prompt(cap: dict[str, Any]) -> str:
    """Build the user-message text from a capability row."""
    meta = cap.get("metadata") or {}
    lines = [
        "Classify this component and return JSON:",
        "{",
        '  "component_kind":  "library|repo|agent|skill|mcp_tool|workflow_template",',
        '  "capability_kind": "library|cli|service|framework|tool|dataset|doc|template",',
        '  "purpose":         "one plain sentence, under 20 words, no marketing language",',
        '  "confidence":      0.0-to-1.0 float,',
        '  "rationale":       "one sentence explaining why"',
        "}",
        "",
        "Component:",
        f"  normalized_key: {cap['normalized_key']}",
        f"  display_name:   {cap['display_name']}",
        f"  current_component_kind:  {cap['component_kind']}",
        f"  current_capability_kind: {cap['kind']}",
    ]
    if meta.get("gemini_description"):
        lines.append(f"  short_description: {meta['gemini_description'][:400]}")
    elif meta.get("description"):
        lines.append(f"  raw_description:   {meta['description'][:400]}")
    if meta.get("seed_note"):
        lines.append(f"  seed_note:  {meta['seed_note'][:200]}")
    if meta.get("topics"):
        lines.append(f"  topics:     {', '.join(meta['topics'][:10])}")
    if meta.get("language"):
        lines.append(f"  language:   {meta['language']}")
    if meta.get("html_url"):
        lines.append(f"  html_url:   {meta['html_url']}")
    lines += [
        "",
        "Rules:",
        "- Return ONLY the JSON object. No markdown, no explanation before or after.",
        "- If evidence is thin, still classify but LOWER confidence toward 0.3-0.5.",
        "- 'component_kind' is the FORMAT (mcp_tool = server exposing MCP tools; ",
        "  skill = LLM instruction file; workflow_template = chain recipe).",
        "- 'capability_kind' is the SEMANTIC ROLE (library = imported; service = ",
        "  runs continuously; framework = opinionated scaffold; tool = single-use CLI).",
    ]
    return "\n".join(lines)


def _prompt_hash(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def call_gemini(prompt_text: str, api_key: str,
                client: Optional[httpx.Client] = None) -> str:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 800,
            "topP": 0.9,
            "responseMimeType": "application/json",
        },
    }
    own = client is None
    if own:
        client = httpx.Client(timeout=45.0)
    try:
        r = client.post(GEMINI_URL, params={"key": api_key}, json=body)
        if r.status_code == 429:
            raise GeminiError(f"rate_limited: {r.text[:200]}")
        if r.status_code >= 400:
            raise GeminiError(f"http_{r.status_code}: {r.text[:200]}")
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            raise GeminiError(f"unexpected_response: {json.dumps(data)[:200]}")
        return text.strip()
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    component_kind: str
    capability_kind: str
    purpose: str
    confidence: float
    rationale: str


def parse_classification(raw: str) -> Classification:
    """Parse and validate the model's JSON response.
    Raises MalformedResponse on any deviation from the schema."""
    # Strip common wrappers (markdown fences) even though we asked for none.
    text = raw.strip()
    if text.startswith("```"):
        # Drop the fence line and closing ```
        text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise MalformedResponse(f"not_json: {e}: {text[:120]}")
    if not isinstance(obj, dict):
        raise MalformedResponse(f"not_object: got {type(obj).__name__}")

    for field in ("component_kind", "capability_kind", "purpose",
                  "confidence", "rationale"):
        if field not in obj:
            raise MalformedResponse(f"missing_field: {field}")

    ck = str(obj["component_kind"]).lower().strip()
    if ck not in VALID_COMPONENT_KINDS:
        raise MalformedResponse(f"bad_component_kind: {ck!r}")
    ck2 = str(obj["capability_kind"]).lower().strip()
    if ck2 not in VALID_CAPABILITY_KINDS:
        raise MalformedResponse(f"bad_capability_kind: {ck2!r}")

    try:
        conf = float(obj["confidence"])
    except (TypeError, ValueError):
        raise MalformedResponse(f"bad_confidence: {obj['confidence']!r}")
    if not 0.0 <= conf <= 1.0:
        raise MalformedResponse(f"confidence_out_of_range: {conf}")

    return Classification(
        component_kind=ck,
        capability_kind=ck2,
        purpose=str(obj["purpose"]).strip()[:280],
        confidence=conf,
        rationale=str(obj["rationale"]).strip()[:280],
    )


# ---------------------------------------------------------------------------
# Selection + persistence
# ---------------------------------------------------------------------------

def _candidates(conn, kind: Optional[str], limit: int,
                force: bool) -> list[dict[str, Any]]:
    """Rows that need classification at the current PROMPT_VERSION."""
    force_filter = "" if force else (
        "AND NOT EXISTS ("
        "  SELECT 1 FROM component_classification cc "
        "  WHERE cc.capability_id = c.id "
        "    AND cc.prompt_name = %(prompt_name)s "
        "    AND cc.prompt_version = %(prompt_version)s"
        ")"
    )
    sql = f"""
        SELECT c.id::text, c.normalized_key, c.display_name,
               c.kind, c.component_kind, c.metadata
        FROM capability c
        WHERE (%(kind)s::text IS NULL OR c.component_kind = %(kind)s)
          {force_filter}
        ORDER BY c.first_seen_at DESC
        LIMIT %(limit)s
    """
    params = {
        "kind": kind, "limit": limit,
        "prompt_name": PROMPT_NAME, "prompt_version": PROMPT_VERSION,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _record_and_maybe_apply(
    conn, cap: dict[str, Any], cls: Classification,
    prompt_hash: str,
) -> str:
    """
    Always record. Apply (mutate capability row) only when confidence
    passes threshold. Returns one of:
      'applied_change' | 'applied_noop' | 'suppressed_low_confidence'.
    """
    prior_comp = cap["component_kind"]
    prior_cap  = cap["kind"]
    differs = (cls.component_kind != prior_comp
               or cls.capability_kind != prior_cap)

    if cls.confidence < APPLY_THRESHOLD:
        applied, apply_reason = False, "suppressed_low_confidence"
    elif not differs:
        applied, apply_reason = False, "no_change"
    else:
        applied, apply_reason = True, "high_confidence"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " proposed_purpose, confidence, rationale, applied, apply_reason, "
            " prior_component_kind, prior_capability_kind, "
            " prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cap["id"], cls.component_kind, cls.capability_kind,
             cls.purpose, cls.confidence, cls.rationale,
             applied, apply_reason, prior_comp, prior_cap,
             PROMPT_NAME, PROMPT_VERSION, prompt_hash, GEMINI_MODEL),
        )
        if applied:
            cur.execute(
                "UPDATE capability SET component_kind = %s, kind = %s WHERE id = %s",
                (cls.component_kind, cls.capability_kind, cap["id"]),
            )

    if applied:
        return "applied_change"
    if apply_reason == "no_change":
        return "applied_noop"
    return "suppressed_low_confidence"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_classification(
    max_calls: int = 30,
    kind: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    interval_sec: float = MIN_INTERVAL_SEC,
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set — cannot classify.")

    conn = connect()
    metadata = {"kind": kind, "max_calls": max_calls, "dry_run": dry_run,
                "force": force, "prompt_version": PROMPT_VERSION,
                "model": GEMINI_MODEL}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            rows = _candidates(conn, kind, max_calls, force)
            print(f"({len(rows)} candidate(s) at prompt v{PROMPT_VERSION})")
            client = httpx.Client(timeout=45.0)
            last_call = 0.0
            try:
                for cap in rows:
                    prompt = _render_prompt(cap)
                    prompt_h = _prompt_hash(prompt)
                    if dry_run:
                        print(f"  dry-run    {cap['normalized_key']}")
                        counts.unchanged += 1
                        continue
                    delta = time.monotonic() - last_call
                    if delta < interval_sec:
                        time.sleep(interval_sec - delta)
                    try:
                        raw = call_gemini(prompt, api_key, client=client)
                        last_call = time.monotonic()
                    except GeminiError as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  gemini_err {cap['normalized_key']}: {e}",
                              file=sys.stderr)
                        if "rate_limited" in str(e):
                            print("    (rate limited; stopping this run)", file=sys.stderr)
                            break
                        continue
                    try:
                        cls = parse_classification(raw)
                    except MalformedResponse as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  malformed  {cap['normalized_key']}: {e}",
                              file=sys.stderr)
                        continue
                    outcome = _record_and_maybe_apply(conn, cap, cls, prompt_h)
                    conn.commit()
                    if outcome == "applied_change":
                        counts.updated += 1
                    elif outcome == "applied_noop":
                        counts.unchanged += 1
                    else:                                    # suppressed
                        counts.unchanged += 1
                    print(f"  {outcome:25s}  {cap['normalized_key']:50s}"
                          f"  -> {cls.component_kind}/{cls.capability_kind}"
                          f" ({cls.confidence:.2f})")
            finally:
                client.close()
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max", type=int, default=30,
                    help="Max Gemini calls this run (default 30).")
    ap.add_argument("--kind", default=None,
                    help="Only classify this component_kind.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List candidates without calling Gemini.")
    ap.add_argument("--force", action="store_true",
                    help="Re-classify even if already classified at current prompt version.")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC,
                    help=f"Min seconds between calls (default {MIN_INTERVAL_SEC}).")
    args = ap.parse_args()
    run_classification(max_calls=args.max, kind=args.kind,
                       dry_run=args.dry_run, force=args.force,
                       interval_sec=args.interval)


if __name__ == "__main__":
    main()
