"""
Connector base — Step 4.

Three obligations, per PDR §9.3, plus one for Step 5 (snapshot).

The interface has NO GitHub-specific type anywhere in its signature. This
is what lets Step 24 (MCP connector) drop in without touching the workflow,
and it's what lets tests substitute a fake connector cleanly.

    enumerate        — find candidate assets by query. One "API call class"
                        of cost.
    current_revision — resolve the current revision of a known asset.
                        MUST be cheap: no content fetch. Step 20's monthly
                        sweep runs this across the entire registry.
    availability     — compare a known revision against reality:
                        unchanged / changed / unavailable / blocked.
                        Also cheap; no analysis.
    snapshot         — yield the file bytes at a revision. This is the
                        one expensive call, and Step 5 is careful to skip
                        it when content_hash already matches.

Nothing else. If a provider has custom operations, they live on the
concrete class and the ingestion worker doesn't call them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Value types — deliberately provider-agnostic
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssetRef:
    """
    A reference to a source asset, in terms both connector and registry
    understand. external_key is whatever uniquely identifies the asset
    at the provider (for GitHub: 'owner/repo').
    """
    provider_name: str
    external_key: str
    kind: str                        # 'repository', 'mcp_server', 'skill', ...
    display_name: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RevisionKey:
    """
    An immutable pointer to a specific point in time on an asset. For
    GitHub this wraps a commit SHA; for others, whatever the provider
    treats as a stable, addressable version.
    """
    value: str

    def __str__(self) -> str:
        return self.value


class AvailabilityStatus(str, Enum):
    UNCHANGED   = "unchanged"        # known revision still current
    CHANGED     = "changed"          # a new revision is available
    UNAVAILABLE = "unavailable"      # provider says the asset is gone
    BLOCKED     = "blocked"          # provider refused (auth, policy)


@dataclass(frozen=True)
class Availability:
    status: AvailabilityStatus
    current_revision: RevisionKey | None = None    # populated when CHANGED
    reason: str | None = None                       # populated when BLOCKED


@dataclass(frozen=True)
class FileEntry:
    """
    One file at a revision. content is present during snapshot; the
    ingestion worker computes the file's own hash and language, then
    discards the bytes unless a storage decision says otherwise.
    """
    path: str                        # POSIX-style, relative to repo root
    size_bytes: int
    content: bytes


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

@runtime_checkable
class SourceConnector(Protocol):
    """
    A connector must expose exactly these four methods. Everything else
    is implementation detail. The workflow calls only these.
    """

    name: str                        # unique registry key, e.g. 'github'

    def enumerate(self, query: str) -> list[AssetRef]: ...

    def current_revision(self, asset: AssetRef) -> RevisionKey: ...

    def availability(
        self,
        asset: AssetRef,
        known_revision: RevisionKey,
    ) -> Availability: ...

    def snapshot(
        self,
        asset: AssetRef,
        revision: RevisionKey,
    ) -> Iterator[FileEntry]: ...


# ---------------------------------------------------------------------------
# Connector-side errors — deliberately three, matching the retry classes
# ---------------------------------------------------------------------------

class ConnectorError(Exception):
    """Base. Never raised directly."""


class TransientConnectorError(ConnectorError):
    """
    Network flap, rate limit, 5xx. Retry with backoff — the workflow
    engine's ErrorClass.TRANSIENT path handles this.
    """


class InputConnectorError(ConnectorError):
    """
    The asset or revision doesn't exist, or the identifier is malformed.
    Retrying won't help — ErrorClass.INPUT.
    """


class BlockedConnectorError(ConnectorError):
    """
    Provider refused: auth, permissions, geo-block, DMCA. Human review —
    ErrorClass.POLICY.
    """


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ConnectorRegistry:
    """
    In-process registry mapping provider name to connector instance.

    Populated at boot from providers.yaml (Step 15). For Steps 4-5 we
    populate it programmatically; the shape is stable so the yaml loader
    drops in later without touching call sites.
    """

    def __init__(self) -> None:
        self._connectors: dict[str, SourceConnector] = {}

    def register(self, connector: SourceConnector) -> None:
        if connector.name in self._connectors:
            raise ValueError(
                f"connector '{connector.name}' already registered"
            )
        self._connectors[connector.name] = connector

    def get(self, name: str) -> SourceConnector:
        try:
            return self._connectors[name]
        except KeyError:
            raise KeyError(f"no connector registered for '{name}'") from None

    def names(self) -> list[str]:
        return sorted(self._connectors.keys())
