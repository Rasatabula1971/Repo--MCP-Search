"""
GitHub connector.

Concrete implementation of SourceConnector for GitHub. Uses the REST API
for enumerate/current_revision/availability (cheap), and the tarball
endpoint for snapshot (expensive, only called from Step 5's fetch stage).

Design notes:
  - current_revision does ONE call: GET /repos/{owner}/{repo}. Returns
    the default branch's HEAD SHA. Step 20's monthly sweep depends on
    this being cheap.
  - snapshot uses GET /repos/{owner}/{repo}/tarball/{ref}. Streams gzip'd
    tarball, expanded in-memory. For repos over a size limit we would
    switch to a shallow git clone; for MVP we assume snapshots fit in RAM.
  - No git clone anywhere. Cloning executes hooks. Tarball is inert.
  - HTTP status classification is deliberate — we map to the same three
    error classes the workflow engine uses.

Auth: fine-grained PAT via GITHUB_TOKEN env var. Anonymous is allowed but
rate-limited hard; the connector runs unauthenticated only in dev and
tests, never in scheduled sweeps (per PDR OD-04).
"""
from __future__ import annotations

import io
import os
import tarfile
from typing import Iterator

import httpx

from connectors.base import (
    AssetRef,
    Availability,
    AvailabilityStatus,
    BlockedConnectorError,
    FileEntry,
    InputConnectorError,
    RevisionKey,
    TransientConnectorError,
)


API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0
MAX_FILE_BYTES = 5 * 1024 * 1024      # skip anything above 5 MB; huge
                                        # binaries aren't evidence
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024  # refuse to load repos above 200 MB
                                        # into memory; a real limit for MVP


def _split_external_key(key: str) -> tuple[str, str]:
    """'owner/repo' -> ('owner', 'repo'). Raises InputConnectorError
    on anything else, since the workflow's retry classifier treats
    input errors as terminal (correctly)."""
    parts = key.split("/")
    if len(parts) != 2 or not all(parts):
        raise InputConnectorError(
            f"malformed github external_key: {key!r} (expected 'owner/repo')"
        )
    return parts[0], parts[1]


class GitHubConnector:
    """
    Implements SourceConnector for GitHub. No inheritance from an ABC —
    duck typing against the Protocol is enough, and the Protocol's
    @runtime_checkable makes isinstance() work if we want it.
    """

    name = "github"

    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        api_root: str = API_ROOT,
    ) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN") or ""
        self._api_root = api_root.rstrip("/")
        # Allow injecting a client for tests. httpx.Client is thread-safe
        # for read-only use of a single Client across requests.
        self._client = client or httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    # -----------------------------------------------------------------
    # Obligation 1: enumerate
    # -----------------------------------------------------------------

    def enumerate(self, query: str) -> list[AssetRef]:
        """
        GET /search/repositories?q={query}
        Returns up to 30 results (one page). Callers wanting more paginate
        by re-querying with a narrower search; we deliberately don't
        auto-paginate here because Step 20's sweeps care about cost.
        """
        resp = self._get("/search/repositories", params={"q": query, "per_page": 30})
        items = resp.get("items", [])
        return [
            AssetRef(
                provider_name=self.name,
                external_key=item["full_name"],
                kind="repository",
                display_name=item.get("name", item["full_name"]),
                metadata={
                    "description": item.get("description") or "",
                    "stars": item.get("stargazers_count", 0),
                    "default_branch": item.get("default_branch", "main"),
                },
            )
            for item in items
        ]

    # -----------------------------------------------------------------
    # Obligation 2: current_revision — MUST be cheap
    # -----------------------------------------------------------------

    def current_revision(self, asset: AssetRef) -> RevisionKey:
        """
        One API call. GET /repos/{owner}/{repo} returns the repo metadata
        including default_branch; then GET /repos/{owner}/{repo}/commits/
        {default_branch} would be a second call. To keep it to ONE call
        we hit /repos/{owner}/{repo}/commits?per_page=1 which returns the
        latest commit on the default branch directly with a single request.
        """
        owner, repo = _split_external_key(asset.external_key)
        data = self._get(
            f"/repos/{owner}/{repo}/commits",
            params={"per_page": 1},
        )
        if not isinstance(data, list) or not data:
            raise InputConnectorError(
                f"no commits found for {asset.external_key}"
            )
        return RevisionKey(value=data[0]["sha"])

    # -----------------------------------------------------------------
    # Obligation 3: availability
    # -----------------------------------------------------------------

    def availability(
        self,
        asset: AssetRef,
        known_revision: RevisionKey,
    ) -> Availability:
        """
        Compare a known revision to what the provider says is current.
        Returns unchanged/changed/unavailable/blocked. Cheap by design —
        this is what the monthly sweep runs.
        """
        try:
            current = self.current_revision(asset)
        except InputConnectorError as exc:
            # 404 on the repo itself is unavailability, not an input
            # error the caller can fix. Distinguish by checking the exc.
            if "no commits found" in str(exc) or "not found" in str(exc).lower():
                return Availability(
                    status=AvailabilityStatus.UNAVAILABLE,
                    reason=str(exc),
                )
            raise
        except BlockedConnectorError as exc:
            return Availability(
                status=AvailabilityStatus.BLOCKED,
                reason=str(exc),
            )

        if current.value == known_revision.value:
            return Availability(status=AvailabilityStatus.UNCHANGED)
        return Availability(
            status=AvailabilityStatus.CHANGED,
            current_revision=current,
        )

    # -----------------------------------------------------------------
    # Obligation 4: snapshot — the one expensive call
    # -----------------------------------------------------------------

    def snapshot(
        self,
        asset: AssetRef,
        revision: RevisionKey,
    ) -> Iterator[FileEntry]:
        """
        Download the tarball at a specific revision and yield each file's
        (path, size, content) tuple.

        Fetching is NOT executing (per Step 5's rule). We open the tarball
        for reading only — no extraction to disk, no execution of any
        scripts inside, no `setup.py install`, no `npm install`. The
        content bytes flow through the ingestion worker's hasher and then
        get discarded.
        """
        owner, repo = _split_external_key(asset.external_key)
        url = f"{self._api_root}/repos/{owner}/{repo}/tarball/{revision.value}"

        # Use a streaming request but read into a bounded BytesIO so we
        # never let a hostile repo balloon memory unbounded.
        headers = self._headers()
        buf = io.BytesIO()
        total = 0
        with self._client.stream("GET", url, headers=headers) as resp:
            _raise_for_status(resp, context=f"tarball {asset.external_key}@{revision}")
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise InputConnectorError(
                        f"tarball for {asset.external_key} exceeds "
                        f"{MAX_ARCHIVE_BYTES} bytes"
                    )
                buf.write(chunk)

        buf.seek(0)
        # gzip'd tar. tarfile with mode 'r:gz' handles it.
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue          # skip dirs, symlinks, devices
                if member.size > MAX_FILE_BYTES:
                    continue          # skip huge blobs
                # GitHub tarballs prefix every path with a top-level
                # 'owner-repo-<sha>/' directory. Strip it so downstream
                # sees repo-relative paths.
                relpath = _strip_leading_component(member.name)
                if not relpath:
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                content = fh.read()
                yield FileEntry(
                    path=relpath,
                    size_bytes=member.size,
                    content=content,
                )

    # -----------------------------------------------------------------
    # HTTP plumbing — one place, one classification of errors
    # -----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str, params: dict | None = None):
        url = f"{self._api_root}{path}"
        try:
            resp = self._client.get(url, params=params, headers=self._headers())
        except httpx.TransportError as exc:
            raise TransientConnectorError(
                f"transport error calling {path}: {exc}"
            ) from exc
        _raise_for_status(resp, context=path)
        return resp.json()


def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
    """
    Map GitHub HTTP responses to the connector's three error classes.
    This is where the retry classifier gets its raw signal — get it wrong
    and either we retry things that will never work, or give up on things
    that would recover.
    """
    if resp.is_success:
        return
    status = resp.status_code
    # 429 and 403-with-rate-limit-header are the two rate-limit shapes.
    if status == 429 or (
        status == 403
        and "rate limit" in (resp.text or "").lower()
    ):
        raise TransientConnectorError(
            f"rate limited on {context} (status {status})"
        )
    if status == 401 or status == 403:
        raise BlockedConnectorError(
            f"auth/permission denied on {context} (status {status})"
        )
    if status == 404:
        raise InputConnectorError(f"not found: {context}")
    if 500 <= status < 600:
        raise TransientConnectorError(
            f"upstream error on {context} (status {status})"
        )
    # Anything else — treat as input error rather than silently retry.
    raise InputConnectorError(
        f"unexpected status {status} on {context}: {resp.text[:200]}"
    )


def _strip_leading_component(path: str) -> str:
    """'psf-requests-abc123/src/x.py' -> 'src/x.py'. Returns '' for the
    top-level directory itself."""
    parts = path.split("/", 1)
    return parts[1] if len(parts) == 2 else ""
