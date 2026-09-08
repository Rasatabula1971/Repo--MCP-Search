"""
Supersession detector (Phase 3c).

For each capability whose description looks like it might announce its
own deprecation ("no longer maintained", "use X instead", "successor
to Y"), asks Gemini:

  1. Is this component deprecated / superseded?
  2. If yes, does the description name a specific successor?

If deprecated + high confidence: marks metadata.deprecated_reason on
the capability (same soft signal the ingester uses for GitHub archived
repos).

If a successor is named AND matches an existing capability by
normalized_key or GitHub URL, also writes a capability_link of kind
'superseded_by' with full provenance.

Idempotent per prompt version — every decision is recorded in
component_classification (reusing that table with prompt_name=
'supersession_detection'), so a re-run at the same prompt version
skips already-asked rows.

Usage:
    python -m scripts.judge.detect_supersession --max 30
    python -m scripts.judge.detect_supersession --max 5 --dry-run
    python -m scripts.judge.detect_supersession --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from db.connection import connect
from scripts.ingest import _base

load_dotenv()

SOURCE_NAME       = "detect_supersession"
GEMINI_MODEL      = "gemini-3.5-flash-lite"
GEMINI_URL        = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)
MIN_INTERVAL_SEC  = 4.0
DEPRECATE_THRESHOLD = 0.75

PROMPT_NAME       = "supersession_detection"
PROMPT_VERSION    = 1


class GeminiError(Exception):
    pass


class MalformedResponse(Exception):
    pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You detect deprecation and supersession signals in software "
    "component descriptions. Respond with STRICT JSON, no fences."
)


def _render_prompt(cap: dict[str, Any]) -> str:
    meta = cap.get("metadata") or {}
    desc = (meta.get("gemini_description") or meta.get("description")
            or meta.get("seed_note") or "")
    prompt = {
        "task": "Detect deprecation and supersession",
        "component": {
            "key": cap["normalized_key"],
            "display_name": cap["display_name"],
            "description": desc[:800] if desc else None,
            "html_url": meta.get("html_url"),
            "archived_at_source": bool(meta.get("archived")),
            "topics": (meta.get("topics") or [])[:8],
        },
        "response_schema": {
            "deprecated": "boolean — is this component itself deprecated/superseded/end-of-life",
            "confidence": "0.0 to 1.0 float",
            "successor_name": "string — name, package key, or URL of the successor, or null if none named",
            "rationale": "one plain sentence citing what in the description signaled it",
        },
        "rules": [
            "Return ONLY the JSON object.",
            "'deprecated' means THIS component is retired, NOT that it deprecates something else.",
            "Absence of a successor is fine — successor_name=null is a valid answer.",
            "If the only signal is 'archived_at_source', that counts as deprecated with high confidence.",
            "Do not infer deprecation from lack of recent activity alone; look for explicit language.",
        ],
    }
    return json.dumps(prompt, indent=2)


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
            "temperature": 0.1,
            "maxOutputTokens": 400,
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
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError):
            raise GeminiError(f"unexpected_response: {json.dumps(data)[:200]}")
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

@dataclass
class SupersessionDecision:
    deprecated: bool
    confidence: float
    successor_name: Optional[str]
    rationale: str


def parse_decision(raw: str) -> SupersessionDecision:
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
    if not isinstance(obj, dict):
        raise MalformedResponse(f"not_object: {type(obj).__name__}")
    for f in ("deprecated", "confidence", "successor_name", "rationale"):
        if f not in obj:
            raise MalformedResponse(f"missing_field: {f}")
    if not isinstance(obj["deprecated"], bool):
        raise MalformedResponse("bad_deprecated: not a bool")
    try:
        conf = float(obj["confidence"])
    except (TypeError, ValueError):
        raise MalformedResponse(f"bad_confidence: {obj['confidence']!r}")
    if not 0.0 <= conf <= 1.0:
        raise MalformedResponse(f"confidence_out_of_range: {conf}")
    succ = obj["successor_name"]
    if succ is not None and not isinstance(succ, str):
        raise MalformedResponse("bad_successor_name: not string or null")
    return SupersessionDecision(
        deprecated=obj["deprecated"],
        confidence=conf,
        successor_name=(succ.strip() if succ else None) or None,
        rationale=str(obj["rationale"]).strip()[:280],
    )


# ---------------------------------------------------------------------------
# Successor resolution
# ---------------------------------------------------------------------------

_GITHUB_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


def _successor_candidates(name: str) -> list[str]:
    """
    From a free-form successor name/URL, produce a list of possible
    normalized_key values to look up in the registry.
    """
    if not name:
        return []
    keys: list[str] = []
    name = name.strip()

    # A github URL -> source:github:owner/name (lowercased)
    m = _GITHUB_URL_RE.search(name)
    if m:
        owner, repo = m.group(1).lower(), m.group(2).lower()
        repo = re.sub(r"\.(git|md)$", "", repo)
        keys.append(f"source:github:{owner}/{repo}")

    # If it looks like owner/name (no schema), also try that.
    if "/" in name and "://" not in name and " " not in name:
        parts = name.split("/", 1)
        if all(re.match(r"^[A-Za-z0-9._-]+$", p) for p in parts):
            keys.append(f"source:github:{parts[0].lower()}/{parts[1].lower()}")

    # Bare token — try pypi and npm.
    if re.match(r"^[a-z0-9][a-z0-9._-]*$", name.lower()):
        keys.append(f"pypi:{name.lower()}")
        keys.append(f"npm:{name.lower()}")

    # Dedup, preserve order.
    seen: set[str] = set()
    unique = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _resolve_successor(conn, name: Optional[str]) -> Optional[str]:
    """Look up the successor in the registry. Returns capability_id or None."""
    if not name:
        return None
    keys = _successor_candidates(name)
    if not keys:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM capability WHERE normalized_key = ANY(%s) LIMIT 1",
            (keys,),
        )
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Selection + persistence
# ---------------------------------------------------------------------------

def _candidates(conn, kind: Optional[str], limit: int,
                force: bool) -> list[dict[str, Any]]:
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


def _record(conn, cap: dict[str, Any], d: SupersessionDecision,
            prompt_hash: str, successor_id: Optional[str]) -> str:
    """
    Record every decision. On deprecated+high-confidence, also patch
    metadata. If a successor resolves, also write a capability_link.
    Returns 'linked_successor' | 'marked_deprecated' | 'no_action' |
    'not_deprecated'.
    """
    # Always store the audit row in component_classification. Proposed
    # component_kind/capability_kind are echoes of current — this table
    # doubles as our "asked at prompt version" ledger for this worker.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO component_classification "
            "(capability_id, proposed_component_kind, proposed_capability_kind, "
            " proposed_purpose, confidence, rationale, applied, apply_reason, "
            " prior_component_kind, prior_capability_kind, "
            " prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cap["id"], cap["component_kind"], cap["kind"],
             ("deprecated: yes" if d.deprecated else "deprecated: no"),
             d.confidence, d.rationale,
             False, "supersession_signal_only",
             cap["component_kind"], cap["kind"],
             PROMPT_NAME, PROMPT_VERSION, prompt_hash, GEMINI_MODEL),
        )

        if not d.deprecated:
            return "not_deprecated"
        if d.confidence < DEPRECATE_THRESHOLD:
            return "no_action"

        # Mark metadata.
        cur.execute(
            "UPDATE capability SET metadata = metadata || %s::jsonb WHERE id = %s",
            (json.dumps({
                "deprecated_reason": "supersession_detected",
                "deprecated_rationale": d.rationale,
                "successor_named": d.successor_name,
            }), cap["id"]),
        )

        if successor_id is None:
            return "marked_deprecated"

        # Write the link. ON CONFLICT DO NOTHING because unique
        # constraint covers re-runs at same prompt version.
        cur.execute(
            "INSERT INTO capability_link "
            "(source_capability_id, target_capability_id, link_kind, "
            " confidence, rationale, prompt_name, prompt_version, "
            " prompt_hash, model_name) "
            "VALUES (%s, %s, 'superseded_by', %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (cap["id"], successor_id, d.confidence, d.rationale,
             PROMPT_NAME, PROMPT_VERSION, prompt_hash, GEMINI_MODEL),
        )
    return "linked_successor"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_detector(
    max_calls: int = 30,
    kind: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    interval_sec: float = MIN_INTERVAL_SEC,
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set.")

    conn = connect()
    metadata = {"kind": kind, "max_calls": max_calls, "dry_run": dry_run,
                "force": force, "prompt_version": PROMPT_VERSION,
                "model": GEMINI_MODEL}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            rows = _candidates(conn, kind, max_calls, force)
            print(f"({len(rows)} candidate(s) at prompt v{PROMPT_VERSION})")
            client = httpx.Client(timeout=45.0)
            last = 0.0
            try:
                for cap in rows:
                    prompt = _render_prompt(cap)
                    ph = _prompt_hash(prompt)
                    if dry_run:
                        print(f"  dry-run    {cap['normalized_key']}")
                        counts.unchanged += 1
                        continue
                    delta = time.monotonic() - last
                    if delta < interval_sec:
                        time.sleep(interval_sec - delta)
                    try:
                        raw = call_gemini(prompt, api_key, client=client)
                        last = time.monotonic()
                    except GeminiError as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  gemini_err {cap['normalized_key']}: {e}",
                              file=sys.stderr)
                        if "rate_limited" in str(e):
                            break
                        continue
                    try:
                        d = parse_decision(raw)
                    except MalformedResponse as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  malformed  {cap['normalized_key']}: {e}",
                              file=sys.stderr)
                        continue
                    successor_id = (_resolve_successor(conn, d.successor_name)
                                     if d.deprecated else None)
                    outcome = _record(conn, cap, d, ph, successor_id)
                    conn.commit()
                    if outcome in ("linked_successor", "marked_deprecated"):
                        counts.deprecated += 1
                    else:
                        counts.unchanged += 1
                    print(f"  {outcome:22s}  {cap['normalized_key']:45s}"
                          f"  ({d.confidence:.2f})"
                          + (f"  -> {d.successor_name}" if d.successor_name else ""))
            finally:
                client.close()
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--kind", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC)
    args = ap.parse_args()
    run_detector(max_calls=args.max, kind=args.kind, dry_run=args.dry_run,
                 force=args.force, interval_sec=args.interval)


if __name__ == "__main__":
    main()
