"""
Phase 2 done-when for the awesome-list ingester:

  "extract_repo_links pulls owner/name from a real README shape,
   deduplicates, drops org-page / topics / assets URLs, strips file
   extensions from names, and preserves first-seen order."

The full run_ingest path is proven live in the commit description.
Extraction is pure and testable offline; that's what this file covers.
"""
from __future__ import annotations

import pytest

from scripts.ingest.awesome_list import extract_repo_links


def test_extracts_simple_repo_links():
    md = """
    - [OpenShot](https://github.com/OpenShot/openshot-qt) — desktop editor
    - [FFmpeg](https://github.com/FFmpeg/FFmpeg) — the media layer
    """
    assert extract_repo_links(md) == [
        ("OpenShot", "openshot-qt"),
        ("FFmpeg", "FFmpeg"),
    ]


def test_deduplicates_repeated_links():
    md = "See https://github.com/foo/bar and later https://github.com/foo/bar again."
    assert extract_repo_links(md) == [("foo", "bar")]


def test_preserves_first_seen_order():
    md = "https://github.com/b/two https://github.com/a/one https://github.com/b/two"
    assert extract_repo_links(md) == [("b", "two"), ("a", "one")]


def test_ignores_org_page_urls():
    md = "https://github.com/OpenShot"  # org page, no repo
    # Regex requires owner/name so this shouldn't match at all.
    assert extract_repo_links(md) == []


def test_ignores_non_repo_paths():
    md = """
    https://github.com/topics/video
    https://github.com/trending
    https://github.com/settings/tokens
    https://github.com/marketplace/actions/foo
    https://github.com/features/copilot
    """
    assert extract_repo_links(md) == []


def test_strips_file_extension_from_name():
    md = """
    https://github.com/foo/bar.git
    https://github.com/baz/qux.md
    """
    assert extract_repo_links(md) == [
        ("foo", "bar"),
        ("baz", "qux"),
    ]


def test_strips_url_path_after_repo():
    md = """
    - https://github.com/foo/bar/blob/main/README.md
    - https://github.com/foo/baz/tree/main/src
    """
    # Only owner/name captured — the extra path is ignored by the regex.
    assert extract_repo_links(md) == [
        ("foo", "bar"),
        ("foo", "baz"),
    ]


def test_handles_mixed_case_owners():
    md = "https://github.com/Some-Org/some-repo"
    assert extract_repo_links(md) == [("Some-Org", "some-repo")]


def test_ignores_urls_without_github_host():
    md = "https://gitlab.com/foo/bar https://bitbucket.org/x/y"
    assert extract_repo_links(md) == []


def test_realistic_readme_mix():
    md = """
    # Awesome Video Production

    A curated list of tools.

    ## Editors
    - [OpenShot](https://github.com/OpenShot/openshot-qt) — Free video editor
    - [Kdenlive](https://github.com/KDE/kdenlive)

    ## See also
    - [awesome-video](https://github.com/krzemienski/awesome-video)

    ## Contributing
    See our [contribution guide](https://github.com/ad-si/awesome-video-production/blob/main/CONTRIBUTING.md).

    ## Topics
    Related: https://github.com/topics/video-editing
    """
    got = extract_repo_links(md)
    assert ("OpenShot", "openshot-qt") in got
    assert ("KDE", "kdenlive") in got
    assert ("krzemienski", "awesome-video") in got
    assert ("ad-si", "awesome-video-production") in got  # self-link kept — dedup happens in run_ingest
    assert ("topics", "video-editing") not in got
