"""
Phase 2 done-when for the GitHub ingester:

  "Running the ingester twice on the same query produces N new + 0
   updated + N unchanged. A repo that gets pushed since the first run
   produces a new revision on the second. An archived repo is
   deprecated. The ingest_run table records both runs with counts and
   the cursor persists for --auto-since."

Test coverage is on ingest_one and _base helpers directly — no HTTP
hits GitHub. The search-page paging path is covered by the smoke
run in the commit description (20 real repos ingested).
"""
from __future__ import annotations

import json
import uuid

import pytest

from scripts.ingest import _base
from scripts.ingest import github_search


# ---------------------------------------------------------------------------
# Fixture: a well-formed GitHub search-result item
# ---------------------------------------------------------------------------

def _item(
    full_name: str = "acme/thing",
    stars: int = 100,
    license_spdx: str = "MIT",
    pushed_at: str = "2026-06-01T00:00:00Z",
    archived: bool = False,
    language: str = "Python",
    default_branch: str = "main",
) -> dict:
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "description": "a thing",
        "stargazers_count": stars,
        "forks_count": 10,
        "open_issues_count": 3,
        "watchers_count": stars,
        "language": language,
        "topics": ["video", "production"],
        "default_branch": default_branch,
        "pushed_at": pushed_at,
        "updated_at": pushed_at,
        "html_url": f"https://github.com/{full_name}",
        "archived": archived,
        "disabled": False,
        "license": {"spdx_id": license_spdx} if license_spdx else None,
    }


# ---------------------------------------------------------------------------
# ingest_one
# ---------------------------------------------------------------------------

def test_ingest_one_writes_repo_capability_with_metadata(conn):
    outcome = github_search.ingest_one(conn, _item("acme/thing", stars=250))
    conn.commit()
    assert outcome == "new"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT display_name, component_kind, runtime, cost_tier, "
            "       license_spdx, metadata "
            "FROM capability WHERE normalized_key = 'source:github:acme/thing'"
        )
        row = cur.fetchone()
    assert row[0] == "acme/thing"
    assert row[1] == "repo"
    assert row[2] == "git_clone"
    assert row[3] == "free"
    assert row[4] == "MIT"
    assert row[5]["stars"] == 250
    assert row[5]["language"] == "Python"


def test_ingest_one_is_idempotent(conn):
    github_search.ingest_one(conn, _item("acme/thing"))
    conn.commit()
    second = github_search.ingest_one(conn, _item("acme/thing"))
    conn.commit()
    assert second == "unchanged"

    # And still only one row.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM capability WHERE normalized_key = 'source:github:acme/thing'")
        assert cur.fetchone()[0] == 1


def test_ingest_one_updates_when_metadata_changes(conn):
    github_search.ingest_one(conn, _item("acme/thing", stars=100))
    conn.commit()
    outcome = github_search.ingest_one(conn, _item("acme/thing", stars=250))
    conn.commit()
    assert outcome == "updated"

    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM capability WHERE normalized_key = 'source:github:acme/thing'")
        assert cur.fetchone()[0]["stars"] == 250


def test_ingest_one_new_revision_when_pushed_at_changes(conn):
    """A repo that got a new commit produces a new source_revision row
    while the capability stays the same — that's how we preserve history."""
    github_search.ingest_one(conn, _item("acme/thing", pushed_at="2026-06-01T00:00:00Z"))
    conn.commit()
    github_search.ingest_one(conn, _item("acme/thing", pushed_at="2026-07-15T00:00:00Z"))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM source_revision r
            JOIN source_asset a ON a.id = r.source_asset_id
            JOIN source_provider p ON p.id = a.provider_id
            WHERE p.name = 'github' AND a.external_key = 'acme/thing'
        """)
        assert cur.fetchone()[0] == 2


def test_ingest_one_archived_repo_is_marked_deprecated(conn):
    outcome = github_search.ingest_one(conn, _item("acme/dead", archived=True))
    conn.commit()
    assert outcome == "deprecated"

    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM capability WHERE normalized_key = 'source:github:acme/dead'")
        meta = cur.fetchone()[0]
    assert meta.get("deprecated_reason") == "github_archived_or_disabled"


def test_ingest_one_license_noassertion_stored_as_null(conn):
    item = _item("acme/nolicense")
    item["license"] = {"spdx_id": "NOASSERTION"}
    github_search.ingest_one(conn, item)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT license_spdx FROM capability WHERE normalized_key = 'source:github:acme/nolicense'")
        assert cur.fetchone()[0] is None


def test_ingest_one_missing_owner_returns_error(conn):
    bad = _item("acme/thing")
    bad["owner"] = None
    outcome = github_search.ingest_one(conn, bad)
    assert outcome == "error"


def test_ingest_one_normalizes_case_in_key(conn):
    """github.com/Acme/Thing and github.com/acme/thing are the same repo
    per GitHub. The normalized_key must lowercase both segments."""
    github_search.ingest_one(conn, _item("Acme/Thing"))
    conn.commit()
    # asset_key preserves case (that's the actual GitHub identifier); only
    # normalized_key is lowercased.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_key FROM capability "
            "WHERE display_name = 'Acme/Thing'"
        )
        assert cur.fetchone()[0] == "source:github:acme/thing"


# ---------------------------------------------------------------------------
# _base helpers
# ---------------------------------------------------------------------------

def test_run_context_records_success(conn):
    with _base.run(conn, "test_source", metadata={"query": "foo"}) as counts:
        counts.new = 5
        counts.unchanged = 2

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, counts, metadata FROM ingest_run "
            "WHERE source_name = 'test_source'"
        )
        row = cur.fetchone()
    assert row[0] == "completed"
    assert row[1] == {"new": 5, "updated": 0, "unchanged": 2, "deprecated": 0, "errors": 0}
    assert row[2]["query"] == "foo"


def test_run_context_records_failure_and_reraises(conn):
    with pytest.raises(RuntimeError, match="boom"):
        with _base.run(conn, "test_source") as counts:
            counts.new = 1
            raise RuntimeError("boom")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_detail, counts FROM ingest_run "
            "WHERE source_name = 'test_source'"
        )
        row = cur.fetchone()
    assert row[0] == "failed"
    assert row[1]["message"] == "boom"
    assert row[2]["new"] == 1


def test_last_cursor_returns_none_when_no_runs(conn):
    assert _base.last_cursor(conn, "never_ran") is None


def test_last_cursor_returns_latest_completed_cursor(conn):
    with _base.run(conn, "src_A", metadata={"cursor": "2026-01-01"}) as _:
        pass
    with _base.run(conn, "src_A", metadata={"cursor": "2026-05-15"}) as _:
        pass
    # A newer failed run should NOT be considered.
    with pytest.raises(RuntimeError):
        with _base.run(conn, "src_A", metadata={"cursor": "2026-08-99"}) as _:
            raise RuntimeError("still failed")

    assert _base.last_cursor(conn, "src_A") == "2026-05-15"


def test_ensure_revision_is_idempotent(conn):
    prov = _base.ensure_provider(conn, "github")
    asset = _base.ensure_asset(conn, prov, "foo/bar")
    r1, new1 = _base.ensure_revision(conn, asset, "sha-abc")
    r2, new2 = _base.ensure_revision(conn, asset, "sha-abc")
    assert r1 == r2
    assert new1 is True
    assert new2 is False


def test_upsert_component_detects_metadata_change(conn):
    _cap_id_1, new_1, upd_1 = _base.upsert_component(
        conn,
        normalized_key="source:github:foo/bar",
        display_name="foo/bar",
        ecosystem="source",
        component_kind="repo",
        metadata={"stars": 100},
    )
    assert new_1 is True and upd_1 is False
    conn.commit()

    _cap_id_2, new_2, upd_2 = _base.upsert_component(
        conn,
        normalized_key="source:github:foo/bar",
        display_name="foo/bar",
        ecosystem="source",
        component_kind="repo",
        metadata={"stars": 250},
    )
    assert new_2 is False and upd_2 is True
