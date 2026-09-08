"""
Cross-kind identity linker (Phase 3b).

Finds pairs of capabilities that MIGHT be the same underlying component
across ecosystems (a pypi package + its github repo; an npm package +
its github repo), asks Gemini "is this the same component?", and
persists the verdict as a `capability_link` row of kind
'same_component'.

Candidate generation is heuristic (shared tail-name across kinds), not
exhaustive. The point of Phase 3b is to catch the obvious matches with
high confidence, not to link every graph edge — anything we miss will
resurface next run as new capabilities arrive.

Idempotent: pairs already asked at the current prompt version are
skipped. Bumping PROMPT_VERSION re-asks everything.

Usage:
    python -m scripts.judge.link_identities --max 30
    python -m scripts.judge.link_identities --max 5 --dry-run
    python -m scripts.judge.link_identities --force
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

SOURCE_NAME       = "link_identities"
GEMINI_MODEL      = "gemini-3.5-flash-lite"
GEMINI_URL        = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)
MIN_INTERVAL_SEC  = 4.0
LINK_THRESHOLD    = 0.75    # write a same_component row above this

PROMPT_NAME       = "same_component_link"
PROMPT_VERSION    = 1


class GeminiError(Exception):
    pass


class MalformedResponse(Exception):
    pass


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^([a-z]+):(?:([^:]+):)?(.+)$")


def _tail_name(normalized_key: str) -> Optional[str]:
    """
    Extract the last segment for matching. Examples:
      pypi:requests                       -> 'requests'
      npm:react-hook-form                 -> 'react-hook-form'
      source:github:psf/requests          -> 'requests'
      source:github:openshot/openshot-qt  -> 'openshot-qt'
    """
    m = _KEY_RE.match(normalized_key)
    if not m:
        return None
    tail = m.group(3)
    if "/" in tail:
        tail = tail.rsplit("/", 1)[-1]
    return tail.lower().strip() or None


def find_link_candidates(
    conn, limit: int, force: bool,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """
    Build a list of (library_row, repo_row) candidate pairs.
    Heuristic: shared tail name between a library and a repo. Excludes
    pairs already asked at current PROMPT_VERSION unless force=True.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, normalized_key, display_name, "
            "       component_kind, kind, metadata "
            "FROM capability "
            "WHERE component_kind IN ('library', 'repo')"
        )
        cols = [d[0] for d in cur.description]
        all_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    libraries = [r for r in all_rows if r["component_kind"] == "library"]
    repos     = [r for r in all_rows if r["component_kind"] == "repo"]

    # Index repos by tail name for fast lookup.
    repos_by_tail: dict[str, list[dict[str, Any]]] = {}
    for r in repos:
        t = _tail_name(r["normalized_key"])
        if t:
            repos_by_tail.setdefault(t, []).append(r)

    pairs: list[tuple[dict, dict]] = []
    for lib in libraries:
        t = _tail_name(lib["normalized_key"])
        if not t or t not in repos_by_tail:
            continue
        for repo in repos_by_tail[t]:
            pairs.append((lib, repo))

    if not pairs:
        return []

    if not force:
        # Filter out pairs we've already asked at this prompt version.
        seen: set[tuple[str, str]] = set()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_capability_id::text, target_capability_id::text "
                "FROM capability_link "
                "WHERE link_kind = 'same_component' "
                "  AND prompt_name = %s AND prompt_version = %s",
                (PROMPT_NAME, PROMPT_VERSION),
            )
            for a, b in cur.fetchall():
                seen.add(_pair_key(a, b))
        pairs = [(l, r) for (l, r) in pairs
                 if _pair_key(l["id"], r["id"]) not in seen]

    return pairs[:limit]


def _pair_key(a_id: str, b_id: str) -> tuple[str, str]:
    """Canonical, unordered key for a pair — same regardless of direction."""
    return tuple(sorted([a_id, b_id]))


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You decide whether two software components are the same real thing "
    "in different ecosystems (e.g. a pypi package and its github repo). "
    "Respond with STRICT JSON. No prose, no fences."
)


def _render_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    def _summ(row):
        meta = row.get("metadata") or {}
        desc = (meta.get("gemini_description")
                or meta.get("description")
                or meta.get("seed_note") or "")
        return {
            "key": row["normalized_key"],
            "display_name": row["display_name"],
            "component_kind": row["component_kind"],
            "description": desc[:400] if desc else None,
            "github_url": meta.get("html_url"),
            "language": meta.get("language"),
        }
    prompt = {
        "task": "Are these the same component?",
        "a": _summ(a),
        "b": _summ(b),
        "response_schema": {
            "same_component": "boolean",
            "confidence": "0.0 to 1.0 float",
            "rationale": "one plain sentence",
        },
        "rules": [
            "Return ONLY the JSON object.",
            "same_component=true only when it's the SAME code/project under different distribution channels.",
            "Package name matching alone is not enough; a repo named /requests that isn't psf/requests is NOT the same as pypi:requests.",
            "If evidence is thin, set confidence 0.3-0.5 and lean false unless the descriptions strongly agree.",
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
class LinkDecision:
    same_component: bool
    confidence: float
    rationale: str


def parse_decision(raw: str) -> LinkDecision:
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
    for f in ("same_component", "confidence", "rationale"):
        if f not in obj:
            raise MalformedResponse(f"missing_field: {f}")
    if not isinstance(obj["same_component"], bool):
        raise MalformedResponse("bad_same_component: not a bool")
    try:
        conf = float(obj["confidence"])
    except (TypeError, ValueError):
        raise MalformedResponse(f"bad_confidence: {obj['confidence']!r}")
    if not 0.0 <= conf <= 1.0:
        raise MalformedResponse(f"confidence_out_of_range: {conf}")
    return LinkDecision(
        same_component=obj["same_component"],
        confidence=conf,
        rationale=str(obj["rationale"]).strip()[:280],
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _record_link(
    conn, a: dict[str, Any], b: dict[str, Any],
    decision: LinkDecision, prompt_hash: str,
) -> str:
    """
    Always insert an audit row (whatever the decision). Only write a
    'same_component' link when same=True AND confidence >= threshold.
    Same=False is a real answer worth remembering so we don't re-ask.
    Returns 'linked' | 'suppressed_low_confidence' | 'not_same'.
    """
    if decision.same_component and decision.confidence >= LINK_THRESHOLD:
        outcome = "linked"
    elif decision.same_component:
        outcome = "suppressed_low_confidence"
    else:
        outcome = "not_same"

    # For 'not_same' we still record a row — as an alternative_to link
    # of NEGATIVE-strength (rationale carries the "no"). That lets the
    # dedup filter in find_link_candidates see it and not re-ask.
    # Simpler: still use link_kind='same_component' but with confidence
    # < threshold and rationale prefixed with 'NOT_SAME:'. The candidate
    # filter checks presence at prompt_version regardless of decision.
    stored_conf = decision.confidence if decision.same_component else (1.0 - decision.confidence)
    rationale = decision.rationale if decision.same_component \
                else f"NOT_SAME: {decision.rationale}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capability_link "
            "(source_capability_id, target_capability_id, link_kind, "
            " confidence, rationale, "
            " prompt_name, prompt_version, prompt_hash, model_name) "
            "VALUES (%s, %s, 'same_component', %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (a["id"], b["id"], stored_conf, rationale,
             PROMPT_NAME, PROMPT_VERSION, prompt_hash, GEMINI_MODEL),
        )
    return outcome


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_linker(
    max_calls: int = 30,
    dry_run: bool = False,
    force: bool = False,
    interval_sec: float = MIN_INTERVAL_SEC,
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set — cannot link.")

    conn = connect()
    metadata = {"max_calls": max_calls, "dry_run": dry_run, "force": force,
                "prompt_version": PROMPT_VERSION, "model": GEMINI_MODEL}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            pairs = find_link_candidates(conn, max_calls, force)
            print(f"({len(pairs)} candidate pair(s) at prompt v{PROMPT_VERSION})")
            client = httpx.Client(timeout=45.0)
            last = 0.0
            try:
                for a, b in pairs:
                    prompt = _render_prompt(a, b)
                    ph = _prompt_hash(prompt)
                    label = f"{a['normalized_key']}  <?>  {b['normalized_key']}"
                    if dry_run:
                        print(f"  dry-run    {label}")
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
                        print(f"  gemini_err {label}: {e}", file=sys.stderr)
                        if "rate_limited" in str(e):
                            print("    (rate limited; stopping)", file=sys.stderr)
                            break
                        continue
                    try:
                        d = parse_decision(raw)
                    except MalformedResponse as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  malformed  {label}: {e}", file=sys.stderr)
                        continue
                    outcome = _record_link(conn, a, b, d, ph)
                    conn.commit()
                    if outcome == "linked":
                        counts.updated += 1
                    else:
                        counts.unchanged += 1
                    print(f"  {outcome:24s}  {label}  ({d.confidence:.2f})")
            finally:
                client.close()
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC)
    args = ap.parse_args()
    run_linker(max_calls=args.max, dry_run=args.dry_run,
               force=args.force, interval_sec=args.interval)


if __name__ == "__main__":
    main()
