"""
Step 4 done-when:
  "The connector interface has no GitHub-specific type in its signature,
   and a fake connector can be substituted in tests."

Also covers the three error classes are mapped correctly from HTTP
status codes, so that the retry classifier in Step 3 gets the right signal.
"""
from __future__ import annotations

import httpx
import pytest

from connectors.base import (
    AssetRef,
    Availability,
    AvailabilityStatus,
    BlockedConnectorError,
    ConnectorRegistry,
    InputConnectorError,
    RevisionKey,
    SourceConnector,
    TransientConnectorError,
)
from connectors.fake import FakeConnector
from connectors.github.client import GitHubConnector


# ---------------------------------------------------------------------------
# Protocol contract — the whole design goal of Step 4
# ---------------------------------------------------------------------------

def test_fake_connector_satisfies_source_connector_protocol():
    fake = FakeConnector()
    assert isinstance(fake, SourceConnector), (
        "FakeConnector must satisfy the Protocol — otherwise the workflow "
        "can't substitute it for GitHubConnector"
    )


def test_github_connector_satisfies_source_connector_protocol():
    gh = GitHubConnector(token="")
    assert isinstance(gh, SourceConnector)


def test_registry_returns_registered_connector():
    reg = ConnectorRegistry()
    fake = FakeConnector(name="fake")
    reg.register(fake)
    assert reg.get("fake") is fake
    assert reg.names() == ["fake"]


def test_registry_rejects_duplicate_registration():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    with pytest.raises(ValueError):
        reg.register(FakeConnector(name="fake"))


def test_registry_missing_connector_raises():
    reg = ConnectorRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


# ---------------------------------------------------------------------------
# FakeConnector semantics — the ingestion tests depend on these
# ---------------------------------------------------------------------------

def test_fake_connector_current_revision_is_latest_added():
    fake = FakeConnector()
    fake.add_revision("psf/requests", "sha_v1", {"a.py": b"x"})
    fake.add_revision("psf/requests", "sha_v2", {"a.py": b"y"})
    asset = AssetRef(provider_name="fake", external_key="psf/requests",
                     kind="repository")
    assert fake.current_revision(asset) == RevisionKey("sha_v2")


def test_fake_connector_availability_matches_known_revision():
    fake = FakeConnector()
    fake.add_revision("psf/requests", "sha_v1", {"a.py": b"x"})
    asset = AssetRef(provider_name="fake", external_key="psf/requests",
                     kind="repository")
    assert fake.availability(asset, RevisionKey("sha_v1")) == Availability(
        status=AvailabilityStatus.UNCHANGED
    )
    fake.add_revision("psf/requests", "sha_v2", {"a.py": b"y"})
    avail = fake.availability(asset, RevisionKey("sha_v1"))
    assert avail.status == AvailabilityStatus.CHANGED
    assert avail.current_revision == RevisionKey("sha_v2")


def test_fake_connector_availability_reports_unavailable_and_blocked():
    fake = FakeConnector()
    unknown = AssetRef(provider_name="fake", external_key="who/what",
                       kind="repository")
    assert fake.availability(unknown, RevisionKey("x")).status == \
        AvailabilityStatus.UNAVAILABLE

    fake.add_revision("blocked/repo", "sha", {"a": b"x"})
    fake.mark_blocked("blocked/repo")
    blocked_asset = AssetRef(provider_name="fake",
                             external_key="blocked/repo",
                             kind="repository")
    assert fake.availability(blocked_asset, RevisionKey("sha")).status == \
        AvailabilityStatus.BLOCKED


def test_fake_connector_snapshot_yields_files_deterministically():
    fake = FakeConnector()
    fake.add_revision("x/y", "s", {"b.py": b"2", "a.py": b"1"})
    asset = AssetRef(provider_name="fake", external_key="x/y",
                     kind="repository")
    entries = list(fake.snapshot(asset, RevisionKey("s")))
    assert [e.path for e in entries] == ["a.py", "b.py"]  # sorted
    assert entries[0].content == b"1"
    assert entries[0].size_bytes == 1


# ---------------------------------------------------------------------------
# GitHub connector HTTP status → error class mapping
# ---------------------------------------------------------------------------

def _github_with_transport(handler):
    """Build a GitHubConnector whose httpx.Client uses a MockTransport."""
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="")
    return GitHubConnector(token="fake", client=client)


def test_github_maps_404_to_input_error():
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"})
    gh = _github_with_transport(handler)
    asset = AssetRef(provider_name="github",
                     external_key="who/what", kind="repository")
    with pytest.raises(InputConnectorError):
        gh.current_revision(asset)


def test_github_maps_401_to_blocked_error():
    def handler(req):
        return httpx.Response(401, json={"message": "Bad credentials"})
    gh = _github_with_transport(handler)
    asset = AssetRef(provider_name="github",
                     external_key="a/b", kind="repository")
    with pytest.raises(BlockedConnectorError):
        gh.current_revision(asset)


def test_github_maps_403_ratelimit_to_transient_error():
    """403 with 'rate limit' in the body is transient, not policy —
    it will succeed if we back off and retry."""
    def handler(req):
        return httpx.Response(
            403,
            text='{"message": "API rate limit exceeded for x"}',
        )
    gh = _github_with_transport(handler)
    asset = AssetRef(provider_name="github",
                     external_key="a/b", kind="repository")
    with pytest.raises(TransientConnectorError):
        gh.current_revision(asset)


def test_github_maps_500_to_transient_error():
    def handler(req):
        return httpx.Response(500, text="Internal Server Error")
    gh = _github_with_transport(handler)
    asset = AssetRef(provider_name="github",
                     external_key="a/b", kind="repository")
    with pytest.raises(TransientConnectorError):
        gh.current_revision(asset)


def test_github_current_revision_reads_first_commit_sha():
    """One API call class — GET /repos/{o}/{r}/commits?per_page=1."""
    captured = {}
    def handler(req):
        captured["url"] = str(req.url)
        return httpx.Response(
            200,
            json=[{"sha": "abc123def456"}],
        )
    gh = _github_with_transport(handler)
    asset = AssetRef(provider_name="github",
                     external_key="psf/requests", kind="repository")
    rev = gh.current_revision(asset)
    assert rev.value == "abc123def456"
    assert "/repos/psf/requests/commits" in captured["url"]
    assert "per_page=1" in captured["url"]


def test_github_malformed_external_key_is_input_error():
    """Not 'owner/repo' — retrying won't fix it."""
    gh = GitHubConnector(token="")
    asset = AssetRef(provider_name="github",
                     external_key="just_one_word", kind="repository")
    with pytest.raises(InputConnectorError):
        gh.current_revision(asset)
