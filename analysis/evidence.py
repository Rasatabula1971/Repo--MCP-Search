"""
Evidence types — Step 6.

An Extractor is anything satisfying the Protocol below. It takes an
iterable of SourceFile and yields EvidenceItem records.

Rules (from the brief, non-negotiable):
  - "Extractors write observations only — interpretation happens later,
    separately."  Extractors don't decide 'this is bad', they say
    'here's a thing at path:line_range'.
  - "Every evidence item resolves to a specific byte range or manifest
    key in a pinned revision. An evidence item that can't be located
    is not evidence."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Input to extractors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFile:
    """
    A file at a snapshotted revision, as supplied to extractors.

    We pass content bytes here — the DB doesn't store them per Step 5's
    rule, but the extractor needs them to look at. Content flows through
    memory and is discarded after extraction.
    """
    path: str                        # repo-relative POSIX path
    content: bytes
    language: str | None = None      # from _detect_language in ingestion


# ---------------------------------------------------------------------------
# Output from extractors
# ---------------------------------------------------------------------------

# The three locator kinds — must match Migration 003's CHECK constraint.
LOCATOR_FILE_RANGE   = "file_range"
LOCATOR_MANIFEST_KEY = "manifest_key"
LOCATOR_WHOLE_FILE   = "whole_file"

VALID_LOCATOR_KINDS = frozenset({
    LOCATOR_FILE_RANGE,
    LOCATOR_MANIFEST_KEY,
    LOCATOR_WHOLE_FILE,
})


@dataclass(frozen=True)
class EvidenceItem:
    """
    One observation from an extractor.

    evidence_type is a short string the interpretation layer (Step 7+)
    knows how to route: 'dependency', 'interface', 'test_indicator',
    'license', 'secret_indicator'.

    locator + locator_kind must resolve back to the source. The
    orchestrator validates this before persisting — see
    validate_locator_or_raise.
    """
    evidence_type: str
    locator_kind: str
    locator: dict
    extracted_value: dict

    def validate_shape(self) -> None:
        """Structural checks that don't require the file list. Raises
        InvalidEvidence on failure."""
        if self.locator_kind not in VALID_LOCATOR_KINDS:
            raise InvalidEvidence(
                f"invalid locator_kind: {self.locator_kind!r}"
            )
        if "path" not in self.locator:
            raise InvalidEvidence(
                f"locator missing 'path': {self.locator!r}"
            )
        if self.locator_kind == LOCATOR_FILE_RANGE:
            if "start_line" not in self.locator or "end_line" not in self.locator:
                raise InvalidEvidence(
                    "file_range locator needs start_line and end_line"
                )
            if self.locator["start_line"] < 1:
                raise InvalidEvidence("start_line must be >= 1")
            if self.locator["end_line"] < self.locator["start_line"]:
                raise InvalidEvidence("end_line must be >= start_line")
        elif self.locator_kind == LOCATOR_MANIFEST_KEY:
            if "key_path" not in self.locator:
                raise InvalidEvidence("manifest_key locator needs key_path")
            if not isinstance(self.locator["key_path"], list):
                raise InvalidEvidence("key_path must be a list")


class InvalidEvidence(ValueError):
    """Raised when an extractor produces an evidence item that violates
    the locator contract. Fail loudly rather than persist bad data."""


# ---------------------------------------------------------------------------
# The Extractor Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Extractor(Protocol):
    """
    An extractor: a named thing that takes files and yields evidence.
    """

    name: str

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]: ...


# ---------------------------------------------------------------------------
# Locator validation against a known file set
# ---------------------------------------------------------------------------

def validate_locator_or_raise(
    item: EvidenceItem,
    known_paths: set[str],
    file_line_counts: dict[str, int] | None = None,
) -> None:
    """
    Check that the item's locator resolves against a real file in the
    revision. The orchestrator calls this before persisting — that's
    what makes 'an evidence item that can't be located is not evidence'
    an enforced invariant, not a hope.

    known_paths: set of paths present in the snapshot.
    file_line_counts: optional {path: line_count} for file_range bounds
                        checking. If not provided, we only check the path
                        exists.
    """
    item.validate_shape()
    path = item.locator["path"]
    if path not in known_paths:
        raise InvalidEvidence(
            f"locator path {path!r} is not in the snapshot"
        )
    if item.locator_kind == LOCATOR_FILE_RANGE and file_line_counts:
        line_count = file_line_counts.get(path)
        if line_count is not None:
            if item.locator["end_line"] > line_count:
                raise InvalidEvidence(
                    f"end_line {item.locator['end_line']} exceeds "
                    f"file line count {line_count} for {path}"
                )
