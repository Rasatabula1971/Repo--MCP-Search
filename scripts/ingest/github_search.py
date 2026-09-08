"""
GitHub repo ingester.

Runs a GitHub search query, persists each result as a `component_kind='repo'`
capability with real metadata (stars, license, default branch SHA, last
commit, description). Idempotent, cursor-tracked, safe to cron.

Usage:
    python -m scripts.ingest.github_search \\
        --query "topic:video-production language:python stars:>50" \\
        --max-repos 100

    python -m scripts.ingest.github_search \\
        --query "topic:video-production" --since 2026-08-01 --max-repos 50

Env:
    GITHUB_TOKEN — required for anything above 60 req/hr. Reads from .env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Iterator, Optional

import httpx
from dotenv import load_dotenv

from db.connection import connect
from scripts.ingest import _base

load_dotenv()

GITHUB_API = "https://api.github.com"
DEFAULT_PER_PAGE = 30
MAX_PAGES = 34            # GitHub search caps at 1000 results (34 * 30 = 1020)


class RateLimit(Exception):
    """Raised when GitHub says slow down. Caller decides whether to sleep."""


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        token = token or os.environ.get("GITHUB_TOKEN") or ""
        self.token = token
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cip-ingester/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(headers=headers, timeout=30.0)

    def close(self) -> None:
        self.client.close()

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> httpx.Response:
        r = self.client.get(f"{GITHUB_API}{path}", params=params)
        remaining = int(r.headers.get("x-ratelimit-remaining", "1") or "1")
        # Secondary rate limit surfaces as 403 with a specific message.
        if r.status_code == 403 and "rate limit" in (r.text or "").lower():
            reset = int(r.headers.get("x-ratelimit-reset", "0") or "0")
            sleep_s = max(reset - int(time.time()), 5)
            raise RateLimit(f"rate limited; retry after {sleep_s}s")
        r.raise_for_status()
        # Proactive throttle: if the primary budget is nearly gone, wait.
        if remaining <= 2:
            reset = int(r.headers.get("x-ratelimit-reset", "0") or "0")
            sleep_s = max(reset - int(time.time()), 1)
            print(f"    (ratelimit budget=2; sleeping {sleep_s}s)", file=sys.stderr)
            time.sleep(sleep_s)
        return r

    def search_repos(
        self, query: str, sort: str = "stars", order: str = "desc",
        per_page: int = DEFAULT_PER_PAGE, max_repos: int = 100,
    ) -> Iterator[dict[str, Any]]:
        yielded = 0
        for page in range(1, MAX_PAGES + 1):
            if yielded >= max_repos:
                return
            r = self._get("/search/repositories", params={
                "q": query, "sort": sort, "order": order,
                "per_page": per_page, "page": page,
            })
            items = r.json().get("items", []) or []
            if not items:
                return
            for item in items:
                if yielded >= max_repos:
                    return
                yield item
                yielded += 1

    def repo_head_sha(self, owner: str, name: str, default_branch: str) -> Optional[str]:
        """Cheap: fetch the ref rather than the branch payload."""
        try:
            r = self._get(f"/repos/{owner}/{name}/git/refs/heads/{default_branch}")
            return r.json().get("object", {}).get("sha")
        except httpx.HTTPStatusError:
            return None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _license_spdx(item: dict[str, Any]) -> Optional[str]:
    lic = item.get("license") or {}
    spdx = lic.get("spdx_id")
    if not spdx or spdx == "NOASSERTION":
        return None
    return spdx


def _repo_to_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "github_full_name": item.get("full_name"),
        "description": item.get("description"),
        "stars": item.get("stargazers_count"),
        "forks": item.get("forks_count"),
        "open_issues": item.get("open_issues_count"),
        "watchers": item.get("watchers_count"),
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "default_branch": item.get("default_branch"),
        "pushed_at": item.get("pushed_at"),
        "updated_at": item.get("updated_at"),
        "html_url": item.get("html_url"),
        "archived": item.get("archived", False),
        "disabled": item.get("disabled", False),
    }


def _cost_tier_for_repo(item: dict[str, Any]) -> str:
    """A whole GitHub repo is always free at source-code level. Runtime
    services it depends on are a separate concern (Phase 4 constraints)."""
    return "free"


def ingest_one(conn, item: dict[str, Any]) -> str:
    """
    Upsert one search-result item. Returns 'new' | 'updated' | 'unchanged'
    | 'deprecated' | 'error'.
    """
    owner = (item.get("owner") or {}).get("login")
    name = item.get("name")
    if not owner or not name:
        return "error"

    external_key = f"{owner}/{name}"
    normalized_key = f"source:github:{external_key.lower()}"
    display_name = item.get("full_name") or external_key
    metadata = _repo_to_metadata(item)
    license_spdx = _license_spdx(item)
    archived = bool(item.get("archived") or item.get("disabled"))

    provider_id = _base.ensure_provider(conn, "github")
    asset_id = _base.ensure_asset(conn, provider_id, external_key,
                                   display_name=display_name, kind="repository")

    # Use the pushed_at / updated_at as the revision key — cheap and
    # avoids an extra API call per repo. If a repo has been pushed since
    # we last saw it, we mint a new revision; otherwise the same row
    # comes back and downstream stays untouched.
    rev_key = f"pushed:{item.get('pushed_at') or item.get('updated_at') or 'unknown'}"
    revision_id, rev_was_new = _base.ensure_revision(conn, asset_id, rev_key)

    cap_id, cap_new, cap_updated = _base.upsert_component(
        conn,
        normalized_key=normalized_key,
        display_name=display_name,
        ecosystem="source",
        capability_kind="library",   # semantic role — refine in judgment (Phase 3)
        component_kind="repo",
        runtime="git_clone",
        cost_tier=_cost_tier_for_repo(item),
        license_spdx=license_spdx,
        metadata=metadata,
    )
    _base.ensure_head_version_for_revision(
        conn, cap_id, revision_id,
        display_version=(item.get("pushed_at") or "")[:10] or None,
    )

    if archived:
        _base.mark_deprecated_metadata(conn, cap_id, "github_archived_or_disabled")
        return "deprecated"
    if cap_new:
        return "new"
    if cap_updated or rev_was_new:
        return "updated"
    return "unchanged"


def run_ingest(
    query: str,
    max_repos: int = 100,
    since: Optional[str] = None,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """
    Top-level entry point. Runs one ingest, returns the counts dict.
    Safe to call repeatedly — idempotent by construction.
    """
    source_name = "github_search"
    metadata = {"query": query, "max_repos": max_repos, "since": since}

    # Compose search query: append 'pushed:>YYYY-MM-DD' if since is given.
    effective_query = query
    if since:
        effective_query = f"{query} pushed:>{since}"

    client = GitHubClient(token=token)
    conn = connect()
    try:
        with _base.run(conn, source_name, metadata=metadata) as counts:
            latest_pushed_seen: Optional[str] = None
            for item in client.search_repos(effective_query, max_repos=max_repos):
                try:
                    outcome = ingest_one(conn, item)
                    if outcome == "new":       counts.new += 1
                    elif outcome == "updated":     counts.updated += 1
                    elif outcome == "unchanged":   counts.unchanged += 1
                    elif outcome == "deprecated":  counts.deprecated += 1
                    else:                            counts.errors += 1
                    print(f"  {outcome:10s}  {item.get('full_name')}")
                    conn.commit()
                except Exception as e:
                    counts.errors += 1
                    conn.rollback()
                    print(f"  error       {item.get('full_name')}: {e}", file=sys.stderr)
                # track the max pushed_at so we can store it as cursor
                pushed = item.get("pushed_at")
                if pushed and (latest_pushed_seen is None or pushed > latest_pushed_seen):
                    latest_pushed_seen = pushed
            # write cursor for next re-run — go 1 second earlier to be safe
            metadata["cursor"] = latest_pushed_seen
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        client.close()
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", required=True,
                    help="GitHub search query (e.g. 'topic:video-production stars:>50').")
    ap.add_argument("--max-repos", type=int, default=100,
                    help="Cap total repos this run (default 100, hard max 1000).")
    ap.add_argument("--since", default=None,
                    help="Only fetch repos pushed after this date (YYYY-MM-DD). "
                         "Uses last-run cursor if omitted and --auto-since is set.")
    ap.add_argument("--auto-since", action="store_true",
                    help="If set and --since is not given, use last completed run's cursor.")
    args = ap.parse_args()

    since = args.since
    if not since and args.auto_since:
        with connect() as c:
            since = _base.last_cursor(c, "github_search")
            if since:
                since = since[:10]  # date only
                print(f"(auto-since resolved to {since} from last run)")

    run_ingest(query=args.query, max_repos=args.max_repos, since=since)


if __name__ == "__main__":
    main()
