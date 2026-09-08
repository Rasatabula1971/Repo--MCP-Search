"""
Gemini enrichment — one-line practical description for every component
whose metadata doesn't already have one.

Reads capability rows, picks ones lacking a `gemini_description` in
metadata, sends a tight prompt to Gemini, and writes the answer back
into metadata. Cached by the SHA-256 of the prompt input, so re-runs
never re-bill for the same source content.

Uses the Gemini REST API directly (no google-generativeai dep). Reads
GEMINI_API_KEY from environment. Per project rule (see memory
feedback-cip-ingestion-and-llm-rules): Gemini first, prompts tight,
cache aggressively.

Usage:
    python -m scripts.ingest.gemini_enrich --max 30
    python -m scripts.ingest.gemini_enrich --max 5 --dry-run
    python -m scripts.ingest.gemini_enrich --kind repo --max 50
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

SOURCE_NAME = "gemini_enrich"
GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

# Free tier: ~15 req/min on 2.5-flash last time we checked. Be a good
# citizen and space calls out.
MIN_INTERVAL_SEC = 4.0


class GeminiError(Exception):
    pass


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = (
    "You describe software components in one plain sentence for a "
    "technical registry. Rules: under 20 words. No marketing language. "
    "No 'This is', no 'A tool for'. Start with a verb (does X) or a "
    "noun phrase (X for Y). If you don't know what it does, reply "
    "exactly with: unknown."
)


def _build_prompt(cap: dict[str, Any]) -> str:
    """Compose the user-message text from a capability row."""
    meta = cap.get("metadata") or {}
    parts = [
        f"normalized_key: {cap['normalized_key']}",
        f"display_name: {cap['display_name']}",
        f"component_kind: {cap['component_kind']}",
    ]
    if meta.get("description"):
        parts.append(f"existing_description: {meta['description'][:400]}")
    if meta.get("seed_note"):
        parts.append(f"seed_note: {meta['seed_note'][:400]}")
    if meta.get("topics"):
        parts.append(f"topics: {', '.join(meta['topics'][:10])}")
    if meta.get("language"):
        parts.append(f"language: {meta['language']}")
    if meta.get("html_url"):
        parts.append(f"html_url: {meta['html_url']}")
    return "\n".join(parts)


def _prompt_hash(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------

def call_gemini(
    prompt_text: str,
    api_key: str,
    client: Optional[httpx.Client] = None,
) -> str:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            # Gemini 3.6 counts thinking tokens against the budget, so 60
            # gets consumed before a single output token appears. 800 is
            # generous headroom; the prompt still asks for one sentence.
            "maxOutputTokens": 800,
            "topP": 0.9,
        },
    }
    own = client is None
    if own:
        client = httpx.Client(timeout=30.0)
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
# Selection + persistence
# ---------------------------------------------------------------------------

def _candidates(conn, kind: Optional[str], limit: int) -> list[dict[str, Any]]:
    """Rows lacking a gemini_description in metadata."""
    sql = """
        SELECT id::text, normalized_key, display_name, component_kind, metadata
        FROM capability
        WHERE (metadata ? 'gemini_description') = FALSE
          AND (%s::text IS NULL OR component_kind = %s)
        ORDER BY first_seen_at DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (kind, kind, limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _write_enrichment(conn, cap_id: str, description: str, prompt_hash: str) -> None:
    patch = {
        "gemini_description": description,
        "gemini_prompt_hash": prompt_hash,
        "gemini_model": GEMINI_MODEL,
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE capability SET metadata = metadata || %s::jsonb WHERE id = %s",
            (json.dumps(patch), cap_id),
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_enrichment(
    max_calls: int = 30,
    kind: Optional[str] = None,
    dry_run: bool = False,
    interval_sec: float = MIN_INTERVAL_SEC,
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set — cannot run enrichment.")

    conn = connect()
    metadata = {"kind": kind, "max_calls": max_calls, "dry_run": dry_run}
    try:
        with _base.run(conn, SOURCE_NAME, metadata=metadata) as counts:
            rows = _candidates(conn, kind, max_calls)
            print(f"({len(rows)} candidate(s) needing enrichment)")
            client = httpx.Client(timeout=30.0)
            last_call = 0.0
            try:
                for cap in rows:
                    prompt = _build_prompt(cap)
                    prompt_h = _prompt_hash(prompt)
                    if dry_run:
                        print(f"  dry-run    {cap['normalized_key']}  (would call Gemini)")
                        counts.unchanged += 1
                        continue
                    # Space calls: no fewer than interval_sec apart.
                    delta = time.monotonic() - last_call
                    if delta < interval_sec:
                        time.sleep(interval_sec - delta)
                    try:
                        answer = call_gemini(prompt, api_key, client=client)
                        last_call = time.monotonic()
                        _write_enrichment(conn, cap["id"], answer, prompt_h)
                        conn.commit()
                        counts.updated += 1
                        preview = answer.replace("\n", " ")[:70]
                        print(f"  enriched   {cap['normalized_key']:60s}  {preview}")
                    except GeminiError as e:
                        counts.errors += 1
                        conn.rollback()
                        print(f"  error      {cap['normalized_key']}: {e}",
                              file=sys.stderr)
                        # If rate-limited hard, bail out — waiting an
                        # unbounded time is worse than resuming later.
                        if "rate_limited" in str(e):
                            print("    (rate limited; stopping this run)", file=sys.stderr)
                            break
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
                    help="Only enrich this component_kind (repo, library, ...).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List candidates without calling Gemini.")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL_SEC,
                    help=f"Min seconds between calls (default {MIN_INTERVAL_SEC}).")
    args = ap.parse_args()
    run_enrichment(max_calls=args.max, kind=args.kind,
                   dry_run=args.dry_run, interval_sec=args.interval)


if __name__ == "__main__":
    main()
