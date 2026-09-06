"""
Step 8 done-when:
  "Re-analysing a new commit produces a new capability_version linked
   to the same capability, with the prior version intact and
   superseded_by_id set."

Test coverage:
  1. normalize_from_files:
     - pyproject.toml [project].name → 'pypi:{canonical}'
     - package.json .name           → 'npm:{name}'
     - PEP 503 canonicalization (case + separator normalization)
     - Fallback → 'source:{provider}:{external_key}'
     - Malformed manifest → falls through, not an error
     - Nested manifests don't beat root ones
  2. Promotion:
     - Creates capability + version + binding + interfaces + deps
     - Idempotent per revision
     - Every interface/dependency has an evidence_item_id (Step 6 rule)
     - Requires snapshotted + analysed revision (fails loudly otherwise)
  3. Supersession (the done-when):
     - Same repo, two commits → same capability, two versions,
       first.superseded_by = second, both rows still exist
     - Different repos, same package name → same capability, two versions,
       NEITHER superseded (independent lineages)
"""
from __future__ import annotations

import uuid

import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.capability.normalize import (
    NormalizedIdentity,
    normalize_from_files,
)
from core.capability.registry import promote_revision
from workers.analysis import run_analysis
from workers.ingestion import ingest_revision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


def _insert_revision(conn, provider_name, external_key, revision_key):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES (%s, 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id",
            (provider_name,),
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, %s, %s, 'repository') "
            "ON CONFLICT (provider_id, external_key) DO UPDATE "
            "SET display_name = EXCLUDED.display_name RETURNING id",
            (provider_id, external_key, external_key),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, %s) RETURNING id",
            (asset_id, revision_key),
        )
        rev_id = cur.fetchone()[0]
    conn.commit()
    return rev_id


def _prep_and_promote(conn, registry, external_key, revision_key, files):
    """End-to-end: seed connector + insert rows + ingest + analyse + promote."""
    fake = registry.get("fake")
    fake.add_revision(external_key, revision_key, files)
    rev_id = _insert_revision(conn, "fake", external_key, revision_key)
    ingest_revision(conn, registry=registry, source_revision_id=rev_id)
    run_analysis(conn, registry=registry, source_revision_id=rev_id)
    outcome = promote_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    return rev_id, outcome


# ---------------------------------------------------------------------------
# normalize_from_files
# ---------------------------------------------------------------------------

def test_normalize_from_pyproject_uses_project_name():
    ident = normalize_from_files(
        provider_name="fake", external_key="acme/lib",
        files={"pyproject.toml":
               b"[project]\nname = 'acme-lib'\nversion = '0.1.0'\n"},
    )
    assert ident.normalized_key == "pypi:acme-lib"
    assert ident.ecosystem == "pypi"
    assert ident.display_name == "acme-lib"


def test_normalize_from_pyproject_canonicalizes_per_pep503():
    """Runs of dot/underscore/hyphen collapse to '-'; lowercased.
    'Some.Package_Name' and 'some-package-name' are the SAME package
    on PyPI, so we key them the same."""
    ident_a = normalize_from_files(
        provider_name="fake", external_key="a/b",
        files={"pyproject.toml":
               b"[project]\nname = 'Some.Package_Name'\n"},
    )
    ident_b = normalize_from_files(
        provider_name="fake", external_key="c/d",
        files={"pyproject.toml":
               b"[project]\nname = 'some-package-name'\n"},
    )
    assert ident_a.normalized_key == ident_b.normalized_key
    assert ident_a.normalized_key == "pypi:some-package-name"


def test_normalize_from_package_json_uses_name():
    ident = normalize_from_files(
        provider_name="fake", external_key="a/react-thing",
        files={"package.json": b'{"name": "React", "version": "1.0"}'},
    )
    assert ident.normalized_key == "npm:react"
    assert ident.ecosystem == "npm"


def test_normalize_falls_back_to_source_when_no_manifest():
    ident = normalize_from_files(
        provider_name="github", external_key="OWNER/repo",
        files={"README.md": b"# hi"},
    )
    # Lowercased so github casing changes don't split identity.
    assert ident.normalized_key == "source:github:owner/repo"
    assert ident.ecosystem == "source"


def test_normalize_falls_back_when_pyproject_lacks_name():
    """A [project] table without a name field isn't grounds to invent
    an identity — fall through to source-based."""
    ident = normalize_from_files(
        provider_name="fake", external_key="a/b",
        files={"pyproject.toml":
               b"[tool.pytest]\naddopts = '-v'\n"},
    )
    assert ident.normalized_key == "source:fake:a/b"


def test_normalize_falls_back_when_manifests_are_malformed():
    ident = normalize_from_files(
        provider_name="fake", external_key="a/b",
        files={"pyproject.toml": b"not valid toml [[[[",
               "package.json": b"not json either {"},
    )
    assert ident.normalized_key == "source:fake:a/b"


def test_normalize_prefers_root_manifest_over_nested():
    """A nested pyproject.toml (e.g. in examples/) shouldn't beat the
    real one at the repo root."""
    ident = normalize_from_files(
        provider_name="fake", external_key="a/b",
        files={
            "pyproject.toml":       b"[project]\nname = 'root-pkg'\n",
            "examples/pyproject.toml": b"[project]\nname = 'nested-pkg'\n",
        },
    )
    assert ident.normalized_key == "pypi:root-pkg"


# ---------------------------------------------------------------------------
# Promotion — basic shape
# ---------------------------------------------------------------------------

_STANDARD_FILES = {
    "pyproject.toml": (
        b"[project]\n"
        b"name = 'acme-lib'\n"
        b"version = '0.1.0'\n"
        b"dependencies = ['httpx>=0.27']\n"
    ),
    "src/acme/__init__.py": b"",
    "src/acme/core.py": (
        b"def greet(name):\n"
        b"    return f'hi {name}'\n"
        b"\n"
        b"class Widget:\n"
        b"    def render(self):\n"
        b"        return 'w'\n"
    ),
    "LICENSE": (
        b"MIT License\n\n"
        b"Permission is hereby granted, free of charge, to any "
        b"person obtaining a copy\n"
    ),
}


def test_promotion_creates_capability_version_binding_and_derived_rows(
    conn, registry
):
    rev_id, outcome = _prep_and_promote(
        conn, registry, "acme/lib", "sha_1", _STANDARD_FILES
    )
    assert outcome.was_noop is False
    assert outcome.normalized_key == "pypi:acme-lib"
    assert outcome.interface_count >= 2       # greet + Widget
    assert outcome.dependency_count >= 1      # httpx

    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_key, ecosystem, kind FROM capability "
            "WHERE id = %s",
            (str(outcome.capability_id),),
        )
        assert cur.fetchone() == ("pypi:acme-lib", "pypi", "library")

        cur.execute(
            "SELECT COUNT(*) FROM capability_source_binding "
            "WHERE capability_version_id = %s AND source_revision_id = %s",
            (str(outcome.capability_version_id), str(rev_id)),
        )
        assert cur.fetchone()[0] == 1


def test_promotion_is_idempotent_for_same_revision(conn, registry):
    rev_id, first = _prep_and_promote(
        conn, registry, "acme/lib", "sha_1", _STANDARD_FILES
    )
    second = promote_revision(
        conn, registry=registry, source_revision_id=rev_id
    )
    assert second.was_noop is True
    assert second.capability_version_id == first.capability_version_id

    # Row counts unchanged.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM capability")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM capability_version")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM capability_source_binding")
        assert cur.fetchone()[0] == 1


def test_every_interface_and_dependency_links_back_to_a_real_evidence_row(
    conn, registry
):
    """The Step 6 non-negotiable, applied at the registry layer: every
    derived attribute must point at an evidence_item that exists."""
    _prep_and_promote(conn, registry, "acme/lib", "sha_1", _STANDARD_FILES)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id
            FROM capability_interface i
            LEFT JOIN evidence_item e ON e.id = i.evidence_item_id
            WHERE e.id IS NULL
            """
        )
        assert cur.fetchall() == []
        cur.execute(
            """
            SELECT d.id
            FROM capability_dependency d
            LEFT JOIN evidence_item e ON e.id = d.evidence_item_id
            WHERE e.id IS NULL
            """
        )
        assert cur.fetchall() == []


def test_promotion_requires_snapshotted_revision(conn, registry):
    """Un-snapshotted revision → fail loudly. Don't silently create an
    empty capability."""
    rev_id = _insert_revision(conn, "fake", "who/what", "sha")
    with pytest.raises(ValueError, match="not snapshotted"):
        promote_revision(
            conn, registry=registry, source_revision_id=rev_id
        )


# ---------------------------------------------------------------------------
# The Step 8 done-when — supersession within a lineage
# ---------------------------------------------------------------------------

def test_two_commits_of_same_repo_produce_supersession_chain(conn, registry):
    """
    THE done-when:
      - re-analysing a new commit produces a NEW capability_version
      - linked to the SAME capability
      - the prior version is intact
      - the prior version's superseded_by_id points at the new one
    """
    # Commit 1
    rev1, out1 = _prep_and_promote(
        conn, registry, "acme/lib", "sha_v1",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"def greet(name):\n    return f'hi {name}'\n"},
    )

    # Commit 2 (same repo, different bytes in one file)
    rev2, out2 = _prep_and_promote(
        conn, registry, "acme/lib", "sha_v2",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"def greet(name):\n    return f'hello {name}'\n"},
    )

    # Same capability.
    assert out1.capability_id == out2.capability_id
    # Distinct versions.
    assert out1.capability_version_id != out2.capability_version_id
    # New promotion recorded which prior version it superseded.
    assert out2.superseded_prior == out1.capability_version_id

    # DB state: two versions, first points at second, both still exist.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, superseded_by_id FROM capability_version "
            "WHERE capability_id = %s ORDER BY created_at",
            (str(out1.capability_id),),
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    v1_id, v1_super = rows[0]
    v2_id, v2_super = rows[1]
    assert v1_id == out1.capability_version_id
    assert v2_id == out2.capability_version_id
    assert v1_super == out2.capability_version_id, (
        "prior version must be superseded by the new one"
    )
    assert v2_super is None, "new version is now the head (no successor)"


def test_three_commits_produce_correct_chain(conn, registry):
    """v1 → v2 → v3. Verifying that supersession chains rather than
    always pointing at v1."""
    _, out1 = _prep_and_promote(
        conn, registry, "acme/lib", "sha_v1",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"# v1\n"},
    )
    _, out2 = _prep_and_promote(
        conn, registry, "acme/lib", "sha_v2",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"# v2\n"},
    )
    _, out3 = _prep_and_promote(
        conn, registry, "acme/lib", "sha_v3",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"# v3\n"},
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, superseded_by_id FROM capability_version "
            "WHERE capability_id = %s ORDER BY created_at",
            (str(out1.capability_id),),
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}

    # v1 -> v2, v2 -> v3, v3 is head
    assert rows[out1.capability_version_id] == out2.capability_version_id
    assert rows[out2.capability_version_id] == out3.capability_version_id
    assert rows[out3.capability_version_id] is None


def test_two_revisions_with_identical_content_share_a_version(conn, registry):
    """
    Independent revisions (different revision_key, could even be
    different repos) that happen to have byte-identical file trees
    produce the same content_hash. They must land on the same
    capability_version — that's what makes content-hash keying
    load-bearing rather than incidental.

    This is what would go wrong if version_key were random: we'd get
    a duplicate version row and split downstream evidence in half.
    """
    rev1, out1 = _prep_and_promote(
        conn, registry, "someone/mirror-a", "sha_a", _STANDARD_FILES
    )
    rev2, out2 = _prep_and_promote(
        conn, registry, "someone/mirror-b", "sha_b", _STANDARD_FILES
    )
    # Same capability (both publish as pypi:acme-lib), same version
    # (identical content), TWO source bindings (one per repo).
    assert out1.capability_id == out2.capability_id
    assert out1.capability_version_id == out2.capability_version_id, (
        "byte-identical revisions must share a capability_version — "
        "version_key must be content-derived, not random"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_source_binding "
            "WHERE capability_version_id = %s",
            (str(out1.capability_version_id),),
        )
        assert cur.fetchone()[0] == 2


def test_different_repos_publishing_same_package_do_not_supersede(
    conn, registry
):
    """
    Two different repos both publish as pypi:acme-lib (a fork situation).
    They land under the SAME capability but each keeps its own version
    head — auto-supersession only applies within a repo lineage.
    """
    _, upstream = _prep_and_promote(
        conn, registry, "acme/lib-upstream", "sha_u1",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"# upstream\n"},
    )
    _, fork = _prep_and_promote(
        conn, registry, "someone/lib-fork", "sha_f1",
        {**_STANDARD_FILES,
         "src/acme/core.py": b"# fork\n"},
    )

    # Same capability.
    assert upstream.capability_id == fork.capability_id
    # Distinct versions.
    assert upstream.capability_version_id != fork.capability_version_id
    # Neither auto-superseded the other.
    assert upstream.superseded_prior is None
    assert fork.superseded_prior is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM capability_version "
            "WHERE capability_id = %s AND superseded_by_id IS NULL",
            (str(upstream.capability_id),),
        )
        head_count = cur.fetchone()[0]
    assert head_count == 2, (
        "two competing repos each maintain their own head; "
        "we do NOT let one auto-supersede the other"
    )
