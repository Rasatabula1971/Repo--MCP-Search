"""
Step 6 done-when:
  "Every evidence item resolves to a specific byte range or manifest key
   in a pinned revision. An evidence item that can't be located is not
   evidence."

Test coverage:
  1. Each extractor produces the expected shapes against fixture files.
  2. The orchestrator's locator validation rejects bad evidence before
     it lands in the DB.
  3. Extractor A raising doesn't stop extractor B — isolation is per-stage.
  4. Re-analysis at the same config version returns the prior run.
  5. evidence_item is append-only (rules block UPDATE and DELETE).
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    LOCATOR_MANIFEST_KEY,
    LOCATOR_WHOLE_FILE,
    EvidenceItem,
    InvalidEvidence,
    SourceFile,
    validate_locator_or_raise,
)
from analysis.extractors.interfaces import InterfaceExtractor
from analysis.extractors.licenses import LicenseExtractor
from analysis.extractors.manifests import ManifestExtractor
from analysis.extractors.secrets import SecretIndicatorExtractor
from analysis.extractors.tests import TestPresenceExtractor
from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from db.connection import connect
from workers.analysis import run_analysis
from workers.ingestion import ingest_revision


# ---------------------------------------------------------------------------
# Fixtures — a snapshotted revision with fixture files ready to analyse
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    reg = ConnectorRegistry()
    reg.register(FakeConnector(name="fake"))
    return reg


@pytest.fixture
def snapshotted_revision(conn, registry):
    """Ingest a fixture revision so analysis has real data to run against."""
    fake = registry.get("fake")
    fake.add_revision("acme/lib", "sha_1", {
        "pyproject.toml": b"""[project]
name = "acme-lib"
version = "0.1.0"
dependencies = [
  "httpx>=0.27",
  "psycopg[binary]==3.2.3",
]

[project.optional-dependencies]
dev = ["pytest>=8"]
""",
        "requirements.txt": b"# runtime\nrequests==2.32.0\nrich\n",
        "src/acme/__init__.py": b"",
        "src/acme/core.py": (
            b"class Widget:\n"
            b"    '''A widget.'''\n"
            b"    def render(self, x: int) -> str:\n"
            b"        return str(x)\n"
            b"\n"
            b"def helper(a, b=1):\n"
            b"    return a + b\n"
            b"\n"
            b"def _private():\n"
            b"    pass\n"
        ),
        "tests/test_core.py": b"def test_helper(): pass\n",
        "LICENSE": (
            b"MIT License\n\n"
            b"Copyright (c) 2026 Acme\n\n"
            b"Permission is hereby granted, free of charge, to any "
            b"person obtaining a copy\n"
        ),
        "README.md": b"# acme-lib\n",
        # A file with what looks like an AWS access key.
        "docs/example.md": (
            b"Example env:\n\n"
            b"    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        ),
    })

    # Insert the provider/asset/revision rows and run ingestion.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, 'acme/lib', 'acme/lib', 'repository') RETURNING id",
            (provider_id,),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'sha_1') RETURNING id",
            (asset_id,),
        )
        rev_id = cur.fetchone()[0]
    conn.commit()

    ingest_revision(conn, registry=registry, source_revision_id=rev_id)
    return rev_id


# ---------------------------------------------------------------------------
# EvidenceItem structural validation
# ---------------------------------------------------------------------------

def test_evidence_item_rejects_unknown_locator_kind():
    item = EvidenceItem(
        evidence_type="x", locator_kind="unknown",
        locator={"path": "a"}, extracted_value={},
    )
    with pytest.raises(InvalidEvidence):
        item.validate_shape()


def test_evidence_item_file_range_needs_lines():
    item = EvidenceItem(
        evidence_type="x", locator_kind=LOCATOR_FILE_RANGE,
        locator={"path": "a.py"}, extracted_value={},
    )
    with pytest.raises(InvalidEvidence):
        item.validate_shape()


def test_evidence_item_end_before_start_is_rejected():
    item = EvidenceItem(
        evidence_type="x", locator_kind=LOCATOR_FILE_RANGE,
        locator={"path": "a.py", "start_line": 10, "end_line": 5},
        extracted_value={},
    )
    with pytest.raises(InvalidEvidence):
        item.validate_shape()


def test_validate_locator_against_known_paths():
    good = EvidenceItem(
        evidence_type="x", locator_kind=LOCATOR_WHOLE_FILE,
        locator={"path": "src/a.py"}, extracted_value={},
    )
    validate_locator_or_raise(good, {"src/a.py"}, {})

    bad = EvidenceItem(
        evidence_type="x", locator_kind=LOCATOR_WHOLE_FILE,
        locator={"path": "does/not/exist.py"}, extracted_value={},
    )
    with pytest.raises(InvalidEvidence):
        validate_locator_or_raise(bad, {"src/a.py"}, {})


def test_validate_locator_rejects_line_beyond_file():
    item = EvidenceItem(
        evidence_type="x", locator_kind=LOCATOR_FILE_RANGE,
        locator={"path": "a.py", "start_line": 1, "end_line": 100},
        extracted_value={},
    )
    with pytest.raises(InvalidEvidence):
        validate_locator_or_raise(item, {"a.py"}, {"a.py": 10})


# ---------------------------------------------------------------------------
# ManifestExtractor
# ---------------------------------------------------------------------------

def test_manifest_extractor_parses_pyproject_dependencies():
    ext = ManifestExtractor()
    files = [SourceFile(
        path="pyproject.toml",
        content=(
            b"[project]\n"
            b"dependencies = ['httpx>=0.27', 'psycopg']\n"
            b"[project.optional-dependencies]\n"
            b"dev = ['pytest']\n"
        ),
        language="toml",
    )]
    items = list(ext.extract(files))
    kinds = [i.extracted_value["kind"] for i in items]
    names = [i.extracted_value["name"] for i in items]
    assert set(names) == {"httpx", "psycopg", "pytest"}
    assert "runtime" in kinds
    assert "optional:dev" in kinds


def test_manifest_extractor_parses_requirements_txt_with_line_locators():
    ext = ManifestExtractor()
    files = [SourceFile(
        path="requirements.txt",
        content=b"# comment\nhttpx==0.27\nrequests\n",
        language=None,
    )]
    items = list(ext.extract(files))
    assert len(items) == 2
    # Line numbers should point to the actual lines.
    assert items[0].locator["start_line"] == 2
    assert items[0].locator["path"] == "requirements.txt"
    assert items[0].extracted_value["name"] == "httpx"


def test_manifest_extractor_parses_package_json():
    ext = ManifestExtractor()
    files = [SourceFile(
        path="package.json",
        content=b'{"dependencies": {"react": "^18"}, '
                b'"devDependencies": {"jest": "^29"}}',
        language="json",
    )]
    items = list(ext.extract(files))
    kinds = {i.extracted_value["name"]: i.extracted_value["kind"]
             for i in items}
    assert kinds == {"react": "runtime", "jest": "dev"}


def test_manifest_extractor_flags_setup_py_without_executing():
    """setup.py evidence has to exist so downstream knows to look manually,
    but nothing about the file's *content* is interpreted."""
    ext = ManifestExtractor()
    files = [SourceFile(
        path="setup.py",
        content=b"from setuptools import setup\nsetup(name='x')\n",
        language="python",
    )]
    items = list(ext.extract(files))
    assert len(items) == 1
    assert items[0].evidence_type == "setup_py_detected"
    assert "not parsed" in items[0].extracted_value["note"]


def test_manifest_extractor_ignores_unrelated_files():
    ext = ManifestExtractor()
    files = [SourceFile(path="README.md", content=b"# hi",
                        language="markdown")]
    assert list(ext.extract(files)) == []


# ---------------------------------------------------------------------------
# InterfaceExtractor
# ---------------------------------------------------------------------------

def test_interface_extractor_finds_public_functions_with_line_ranges():
    ext = InterfaceExtractor()
    src = (
        b"def public(a, b=1):\n"
        b"    return a + b\n"
        b"\n"
        b"def _hidden():\n"
        b"    pass\n"
    )
    items = list(ext.extract([
        SourceFile(path="m.py", content=src, language="python")
    ]))
    assert len(items) == 1
    fn = items[0]
    assert fn.extracted_value["name"] == "public"
    assert fn.extracted_value["kind"] == "function"
    assert fn.extracted_value["signature"] == "a, b=1"
    assert fn.locator["start_line"] == 1
    assert fn.locator["end_line"] == 2


def test_interface_extractor_extracts_classes_and_public_methods():
    ext = InterfaceExtractor()
    src = (
        b"class Widget:\n"
        b"    def render(self): pass\n"
        b"    def _internal(self): pass\n"
    )
    items = list(ext.extract([
        SourceFile(path="w.py", content=src, language="python")
    ]))
    assert len(items) == 1
    cls = items[0]
    assert cls.extracted_value["kind"] == "class"
    assert cls.extracted_value["name"] == "Widget"
    assert cls.extracted_value["public_methods"] == ["render"]


def test_interface_extractor_skips_syntax_errors_gracefully():
    ext = InterfaceExtractor()
    items = list(ext.extract([
        SourceFile(path="bad.py", content=b"def broken(",
                   language="python")
    ]))
    assert items == []


def test_interface_extractor_ignores_non_python_files():
    ext = InterfaceExtractor()
    items = list(ext.extract([
        SourceFile(path="a.js", content=b"function foo(){}",
                   language="javascript")
    ]))
    assert items == []


# ---------------------------------------------------------------------------
# TestPresenceExtractor
# ---------------------------------------------------------------------------

def test_test_presence_recognises_pytest_conventions():
    ext = TestPresenceExtractor()
    files = [
        SourceFile(path="tests/test_core.py", content=b"", language="python"),
        SourceFile(path="conftest.py", content=b"", language="python"),
        SourceFile(path="src/core_test.py", content=b"", language="python"),
        SourceFile(path="src/core.py", content=b"", language="python"),  # not a test
    ]
    items = list(ext.extract(files))
    assert len(items) == 3
    assert all(i.evidence_type == "test_indicator" for i in items)
    paths = {i.locator["path"] for i in items}
    assert paths == {"tests/test_core.py", "conftest.py", "src/core_test.py"}


def test_test_presence_recognises_js_and_go_conventions():
    ext = TestPresenceExtractor()
    files = [
        SourceFile(path="src/foo.test.ts", content=b"", language="typescript"),
        SourceFile(path="pkg/thing_test.go", content=b"", language="go"),
    ]
    items = list(ext.extract(files))
    assert {i.extracted_value["ecosystem"] for i in items} == {"js", "go"}


# ---------------------------------------------------------------------------
# LicenseExtractor
# ---------------------------------------------------------------------------

def test_license_extractor_detects_mit_at_high_confidence():
    ext = LicenseExtractor()
    files = [SourceFile(
        path="LICENSE",
        content=(
            b"MIT License\n\n"
            b"Permission is hereby granted, free of charge, to any "
            b"person obtaining a copy\n"
        ),
        language=None,
    )]
    items = list(ext.extract(files))
    assert len(items) == 1
    assert items[0].extracted_value["spdx_id"] == "MIT"
    assert items[0].extracted_value["confidence"] == "high"


def test_license_extractor_detects_apache_2():
    ext = LicenseExtractor()
    files = [SourceFile(
        path="LICENSE.txt",
        content=b"Licensed under the Apache License, Version 2.0 (the License)",
        language=None,
    )]
    items = list(ext.extract(files))
    assert items[0].extracted_value["spdx_id"] == "Apache-2.0"


def test_license_extractor_emits_unknown_when_content_doesnt_match():
    ext = LicenseExtractor()
    files = [SourceFile(
        path="LICENSE",
        content=b"You may do whatever, no warranty, blah blah.\n",
        language=None,
    )]
    items = list(ext.extract(files))
    assert len(items) == 1
    assert items[0].extracted_value["spdx_id"] == "unknown"


def test_license_extractor_ignores_deep_license_files():
    """A LICENSE inside vendored code shouldn't be read as THE license
    for the whole repo."""
    ext = LicenseExtractor()
    files = [SourceFile(
        path="vendor/some-lib/LICENSE",
        content=b"MIT License",
        language=None,
    )]
    assert list(ext.extract(files)) == []


# ---------------------------------------------------------------------------
# SecretIndicatorExtractor
# ---------------------------------------------------------------------------

def test_secret_indicator_catches_aws_access_key_and_redacts():
    ext = SecretIndicatorExtractor()
    files = [SourceFile(
        path="cfg/env.md",
        content=b"AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        language="markdown",
    )]
    items = list(ext.extract(files))
    assert len(items) == 1
    ev = items[0]
    assert ev.extracted_value["pattern_name"] == "aws_access_key"
    # Redacted preview keeps 4 chars, hides the rest.
    preview = ev.extracted_value["redacted_preview"]
    assert preview.startswith("AKIA")
    assert "*" in preview
    assert "AKIAIOSFODNN7EXAMPLE" not in preview


def test_secret_indicator_catches_github_pat():
    ext = SecretIndicatorExtractor()
    files = [SourceFile(
        path="script.sh",
        content=b"export TOKEN=ghp_" + b"A" * 36,
        language="shell",
    )]
    items = list(ext.extract(files))
    assert len(items) == 1
    assert items[0].extracted_value["pattern_name"] == "github_pat"


def test_secret_indicator_skips_binary_files():
    """A file with NUL bytes near the start should be treated as binary
    and skipped — not scanned for text patterns."""
    ext = SecretIndicatorExtractor()
    files = [SourceFile(
        path="build/thing.bin",
        content=b"\x00\x01\x02\x03" * 100 + b"AKIAIOSFODNN7EXAMPLE",
        language=None,
    )]
    assert list(ext.extract(files)) == []


# ---------------------------------------------------------------------------
# Orchestrator — end-to-end analysis run
# ---------------------------------------------------------------------------

def test_analysis_produces_evidence_across_extractors(
    conn, registry, snapshotted_revision
):
    outcome = run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision
    )
    assert outcome.was_noop is False
    assert outcome.total_evidence_count > 0

    # Every stage completed.
    assert all(s.status == "succeeded" for s in outcome.stages)

    # We can identify each extractor's evidence types in the DB.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT extractor_name, evidence_type "
            "FROM evidence_item WHERE source_revision_id = %s "
            "ORDER BY extractor_name, evidence_type",
            (str(snapshotted_revision),),
        )
        pairs = cur.fetchall()

    names = {p[0] for p in pairs}
    types = {p[1] for p in pairs}
    assert "manifests" in names
    assert "interfaces" in names
    assert "test_presence" in names
    assert "license" in names
    assert "secret_indicators" in names
    assert {"dependency", "interface", "test_indicator", "license",
            "secret_indicator"} <= types


def test_every_evidence_item_locator_resolves_to_a_real_file(
    conn, registry, snapshotted_revision
):
    """
    The Step 6 done-when, exactly. For every evidence_item row, the
    locator's path must exist in file_artifact for the same revision.
    """
    run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.locator->>'path' AS locator_path
            FROM evidence_item e
            LEFT JOIN file_artifact f
              ON f.source_revision_id = e.source_revision_id
             AND f.path = e.locator->>'path'
            WHERE e.source_revision_id = %s
              AND f.id IS NULL
            """,
            (str(snapshotted_revision),),
        )
        orphans = cur.fetchall()
    assert orphans == [], (
        f"evidence rows whose locator path is not in file_artifact: "
        f"{orphans}"
    )


def test_reanalysis_at_same_config_version_is_noop(
    conn, registry, snapshotted_revision
):
    first = run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision
    )
    fake = registry.get("fake")
    fake.reset_call_counts()

    second = run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision
    )
    assert second.was_noop is True
    assert second.analysis_run_id == first.analysis_run_id
    assert second.total_evidence_count == first.total_evidence_count
    # No snapshot fetched — no work at all.
    assert fake.call_counts["snapshot"] == 0

    # Only ONE analysis_run row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM analysis_run "
            "WHERE source_revision_id = %s",
            (str(snapshotted_revision),),
        )
        assert cur.fetchone()[0] == 1


def test_reanalysis_at_new_config_version_creates_new_run(
    conn, registry, snapshotted_revision
):
    """Bumping the config version invalidates the no-op cache — a fresh
    run happens even for the same content."""
    run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision,
        extractor_config_version=1,
    )
    second = run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision,
        extractor_config_version=2,
    )
    assert second.was_noop is False

    with conn.cursor() as cur:
        cur.execute(
            "SELECT extractor_config_version FROM analysis_run "
            "WHERE source_revision_id = %s ORDER BY created_at",
            (str(snapshotted_revision),),
        )
        versions = [r[0] for r in cur.fetchall()]
    assert versions == [1, 2]


def test_extractor_failure_isolated_other_stages_still_complete(
    conn, registry, snapshotted_revision
):
    """One extractor raising must not stop the rest. Its stage_result is
    marked failed with the error; other stages complete normally."""

    class ExplodingExtractor:
        name = "exploder"
        def extract(self, files):
            raise RuntimeError("boom")

    outcome = run_analysis(
        conn, registry=registry,
        source_revision_id=snapshotted_revision,
        extractors=[
            ManifestExtractor(),
            ExplodingExtractor(),
            LicenseExtractor(),
        ],
    )
    by_name = {s.extractor_name: s for s in outcome.stages}
    assert by_name["manifests"].status == "succeeded"
    assert by_name["exploder"].status == "failed"
    assert "boom" in by_name["exploder"].error_detail["error"]
    assert by_name["license"].status == "succeeded"

    # The DB shows the same: three stage_result rows, one failed.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extractor_name, status FROM analysis_stage_result "
            "WHERE analysis_run_id = %s ORDER BY started_at",
            (str(outcome.analysis_run_id),),
        )
        rows = cur.fetchall()
    statuses = dict(rows)
    assert statuses == {
        "manifests": "succeeded",
        "exploder": "failed",
        "license": "succeeded",
    }


def test_extractor_producing_invalid_locator_fails_its_stage_cleanly(
    conn, registry, snapshotted_revision
):
    """
    'An evidence item that can't be located is not evidence' — enforced.
    An extractor that emits an item pointing to a non-existent path fails
    the stage; no partial evidence is persisted for that stage.
    """

    class BadLocatorExtractor:
        name = "bad_locator"
        def extract(self, files):
            yield EvidenceItem(
                evidence_type="test",
                locator_kind=LOCATOR_WHOLE_FILE,
                locator={"path": "no/such/file.py"},
                extracted_value={},
            )

    outcome = run_analysis(
        conn, registry=registry,
        source_revision_id=snapshotted_revision,
        extractors=[BadLocatorExtractor()],
    )
    by_name = {s.extractor_name: s for s in outcome.stages}
    assert by_name["bad_locator"].status == "failed"
    assert "not in the snapshot" in by_name["bad_locator"].error_detail["error"]

    # And nothing from that stage landed.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM evidence_item "
            "WHERE analysis_run_id = %s AND extractor_name = 'bad_locator'",
            (str(outcome.analysis_run_id),),
        )
        assert cur.fetchone()[0] == 0


def test_evidence_item_is_append_only(conn, registry, snapshotted_revision):
    run_analysis(
        conn, registry=registry, source_revision_id=snapshotted_revision
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, extracted_value FROM evidence_item "
            "WHERE source_revision_id = %s LIMIT 1",
            (str(snapshotted_revision),),
        )
        row = cur.fetchone()
    assert row is not None, "expected at least one evidence row"
    ev_id = row[0]

    # UPDATE and DELETE are rewritten to no-ops by the rules.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE evidence_item SET extracted_value = '{}'::jsonb "
            "WHERE id = %s", (str(ev_id),)
        )
        cur.execute("DELETE FROM evidence_item WHERE id = %s", (str(ev_id),))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, extracted_value FROM evidence_item WHERE id = %s",
            (str(ev_id),),
        )
        after = cur.fetchone()
    assert after is not None, "DELETE should have been silently ignored"
    assert after[1] == row[1], "UPDATE should have been silently ignored"


def test_analysis_requires_snapshotted_revision(conn, registry):
    """You can't analyse an un-snapshotted revision. Fail loudly."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, 'a/b', 'a/b', 'repository') RETURNING id",
            (provider_id,),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'sha') RETURNING id",
            (asset_id,),
        )
        rev_id = cur.fetchone()[0]
    conn.commit()

    with pytest.raises(ValueError, match="not snapshotted"):
        run_analysis(conn, registry=registry, source_revision_id=rev_id)
