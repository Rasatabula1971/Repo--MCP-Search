"""
Awesome-list ingester.

Takes an owner/repo of an `awesome-*` list, fetches its README from
GitHub's raw content endpoint, extracts every `github.com/owner/name`
link, and feeds each one to the github_search ingester as a targeted
single-repo fetch.

Cheap way to seed the registry with hundreds of curated, on-topic repos
in one run.

Usage:
    python -m scripts.ingest.awesome_list \\
        --list ad-si/awesome-video-production

    python -m scripts.ingest.awesome_list \\
        --list ad-si/awesome-video-production --max-repos 50

Env:
    GITHUB_TOKEN — required for anything above 60 req/hr.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Optional

import httpx

from db.connection import connect
from scripts.ingest import _base
from scripts.ingest.github_search import GitHubClient, ingest_one

# github.com/owner/name — with optional path (which we strip) and optional
# fragment/query (which we also strip). Owner and name are the standard
# GitHub character set. We refuse organizations-only (no repo) URLs.
LINK_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"([A-Za-z0-9._-]{1,100})",
)

# Paths that look like `github.com/owner` (org page), `github.com/topics/foo`,
# `github.com/settings`, etc. must be filtered — they parsed as owner/name
# but aren't repos.
NON_REPO_OWNERS = {
    "topics", "trending", "settings", "orgs", "notifications", "features",
    "marketplace", "pricing", "about", "collections", "explore", "sponsors",
    "readme", "search", "login", "join", "pulls", "issues", "stars",
    "codespaces", "new", "organizations", "site", "assets", "blog",
    "actions",
}


def extract_repo_links(readme_text: str) -> list[tuple[str, str]]:
    """Return a de-duplicated list of (owner, name) tuples in first-seen order."""
    seen: set[tuple[str, str]] = set()
    order: list[tuple[str, str]] = []
    for m in LINK_RE.finditer(readme_text):
        owner, name = m.group(1), m.group(2)
        # Strip trailing `.git`, `.md`, `.png` etc.
        name = re.sub(r"\.(git|md|png|jpg|jpeg|svg|gif)$", "", name)
        if owner.lower() in NON_REPO_OWNERS:
            continue
        key = (owner, name)
        if key in seen:
            continue
        seen.add(key)
        order.append(key)
    return order


def fetch_readme(client: GitHubClient, list_repo: str) -> str:
    """Fetch the README for `owner/repo` via the GitHub API. Follows the
    server-selected default branch, so we don't have to guess."""
    r = client.client.get(
        f"https://api.github.com/repos/{list_repo}/readme",
        headers={"Accept": "application/vnd.github.raw"},
    )
    r.raise_for_status()
    return r.text


def fetch_repo_metadata(
    client: GitHubClient, owner: str, name: str,
) -> Optional[dict]:
    """Return a search-result-shaped dict for one repo, or None if 404.
    Uses /repos/{owner}/{name} — that shape is a strict superset of the
    search result shape, so ingest_one takes it as-is."""
    try:
        r = client.client.get(f"https://api.github.com/repos/{owner}/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        print(f"    ({owner}/{name}: HTTP {e.response.status_code})", file=sys.stderr)
        return None


def run_ingest(
    list_repo: str,
    max_repos: Optional[int] = None,
    token: Optional[str] = None,
) -> dict:
    """
    Ingest one awesome list. Returns the counts dict.
    Safe to re-run — each linked repo goes through ingest_one which is
    idempotent.
    """
    source_name = f"awesome_list:{list_repo}"
    client = GitHubClient(token=token)
    conn = connect()
    try:
        # README first — 1 API call. Extraction is offline.
        readme = fetch_readme(client, list_repo)
        links = extract_repo_links(readme)
        # Drop self-referential link to the list itself.
        list_owner, list_name = list_repo.split("/", 1)
        links = [(o, n) for (o, n) in links
                 if (o.lower(), n.lower()) != (list_owner.lower(), list_name.lower())]
        if max_repos:
            links = links[:max_repos]
        print(f"({len(links)} candidate repos extracted from {list_repo})")

        metadata = {"list_repo": list_repo, "extracted_count": len(links)}
        with _base.run(conn, source_name, metadata=metadata) as counts:
            for owner, name in links:
                item = fetch_repo_metadata(client, owner, name)
                if item is None:
                    counts.errors += 1
                    print(f"  skip404     {owner}/{name}")
                    continue
                try:
                    outcome = ingest_one(conn, item)
                    if outcome == "new":         counts.new += 1
                    elif outcome == "updated":     counts.updated += 1
                    elif outcome == "unchanged":   counts.unchanged += 1
                    elif outcome == "deprecated":  counts.deprecated += 1
                    else:                            counts.errors += 1
                    print(f"  {outcome:10s}  {owner}/{name}")
                    conn.commit()
                except Exception as e:
                    counts.errors += 1
                    conn.rollback()
                    print(f"  error       {owner}/{name}: {e}", file=sys.stderr)
            metadata["cursor"] = None  # awesome-list has no natural time cursor
            print(f"\n{counts.as_dict()}")
        return counts.as_dict()
    finally:
        client.close()
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", required=True,
                    help="owner/repo of the awesome list (e.g. 'ad-si/awesome-video-production').")
    ap.add_argument("--max-repos", type=int, default=None,
                    help="Cap total repos this run.")
    args = ap.parse_args()

    run_ingest(list_repo=args.list, max_repos=args.max_repos)


if __name__ == "__main__":
    main()
