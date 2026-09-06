"""
FakeConnector — an in-memory SourceConnector for tests.

The point: exercise the ingestion worker and workflow engine against
something deterministic, so tests don't depend on GitHub being up or on
a token being present.

If the real GitHub connector and this one both satisfy SourceConnector,
the ingestion worker can't tell them apart. That's the whole design goal
of Step 4: "the connector interface has no GitHub-specific type in its
signature, and a fake connector can be substituted in tests."
"""
from __future__ import annotations

from typing import Iterator

from connectors.base import (
    AssetRef,
    Availability,
    AvailabilityStatus,
    FileEntry,
    InputConnectorError,
    RevisionKey,
)


class FakeConnector:
    """
    A fake connector keyed on external_key. Register test data with
    `add_revision(external_key, revision_key, files)`; the four obligation
    methods then work against that data.

    Also records call counts, so tests can assert that current_revision
    was called exactly once during a no-op re-ingestion (Step 5's
    'done when' bar).
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        # external_key -> [(revision_key, {path: content})], newest last
        self._revisions: dict[str, list[tuple[str, dict[str, bytes]]]] = {}
        self._blocked: set[str] = set()
        self._reverse_snapshot_order = False
        self.call_counts: dict[str, int] = {
            "enumerate": 0,
            "current_revision": 0,
            "availability": 0,
            "snapshot": 0,
        }

    def set_reverse_snapshot_order(self, value: bool = True) -> None:
        """Yield files reverse-sorted instead of sorted. Used to prove
        the revision content_hash is order-independent."""
        self._reverse_snapshot_order = value

    # ------------------------------------------------------------
    # Test setup helpers
    # ------------------------------------------------------------

    def add_revision(
        self,
        external_key: str,
        revision_key: str,
        files: dict[str, bytes],
    ) -> None:
        self._revisions.setdefault(external_key, []).append(
            (revision_key, dict(files))
        )

    def mark_blocked(self, external_key: str) -> None:
        self._blocked.add(external_key)

    def reset_call_counts(self) -> None:
        for key in self.call_counts:
            self.call_counts[key] = 0

    # ------------------------------------------------------------
    # SourceConnector protocol
    # ------------------------------------------------------------

    def enumerate(self, query: str) -> list[AssetRef]:
        self.call_counts["enumerate"] += 1
        return [
            AssetRef(
                provider_name=self.name,
                external_key=key,
                kind="repository",
                display_name=key,
            )
            for key in sorted(self._revisions)
            if query.lower() in key.lower()
        ]

    def current_revision(self, asset: AssetRef) -> RevisionKey:
        self.call_counts["current_revision"] += 1
        if asset.external_key in self._blocked:
            # Fake blocks are surfaced via availability, not current_revision,
            # to mirror how GitHub 403s can arrive here.
            from connectors.base import BlockedConnectorError
            raise BlockedConnectorError(f"blocked: {asset.external_key}")
        history = self._revisions.get(asset.external_key)
        if not history:
            raise InputConnectorError(
                f"unknown external_key: {asset.external_key}"
            )
        return RevisionKey(value=history[-1][0])

    def availability(
        self,
        asset: AssetRef,
        known_revision: RevisionKey,
    ) -> Availability:
        self.call_counts["availability"] += 1
        if asset.external_key in self._blocked:
            return Availability(
                status=AvailabilityStatus.BLOCKED,
                reason="fake block",
            )
        history = self._revisions.get(asset.external_key)
        if not history:
            return Availability(
                status=AvailabilityStatus.UNAVAILABLE,
                reason="unknown",
            )
        current = history[-1][0]
        if current == known_revision.value:
            return Availability(status=AvailabilityStatus.UNCHANGED)
        return Availability(
            status=AvailabilityStatus.CHANGED,
            current_revision=RevisionKey(value=current),
        )

    def snapshot(
        self,
        asset: AssetRef,
        revision: RevisionKey,
    ) -> Iterator[FileEntry]:
        self.call_counts["snapshot"] += 1
        if asset.external_key in self._blocked:
            from connectors.base import BlockedConnectorError
            raise BlockedConnectorError(f"blocked: {asset.external_key}")
        history = self._revisions.get(asset.external_key, [])
        for rev_key, files in history:
            if rev_key == revision.value:
                paths = sorted(files, reverse=self._reverse_snapshot_order)
                for path in paths:
                    content = files[path]
                    yield FileEntry(
                        path=path,
                        size_bytes=len(content),
                        content=content,
                    )
                return
        raise InputConnectorError(
            f"revision {revision.value} not found on {asset.external_key}"
        )
