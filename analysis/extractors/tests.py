"""
Test presence extractor.

Signals presence of tests. Does NOT run them, count assertions, or judge
quality — that's interpretation, not observation.

For each file that matches a common test convention we emit a
'test_indicator' evidence item with a whole_file locator. Interpretation
(Step 9's scoring, Step 10's gates) turns "N test indicators exist" into
"has tests: yes/no" or "test coverage: heuristic score".
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_WHOLE_FILE,
    EvidenceItem,
    SourceFile,
)


# Patterns per ecosystem. Each matches the whole POSIX path.
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # Python
    ("python", "pytest_conftest",  re.compile(r"(^|/)conftest\.py$")),
    ("python", "pytest_test_file", re.compile(r"(^|/)test_[^/]+\.py$")),
    ("python", "pytest_file_test", re.compile(r"(^|/)[^/]+_test\.py$")),
    ("python", "tests_dir_file",   re.compile(r"(^|/)tests?/[^/]+\.py$")),
    # JavaScript / TypeScript
    ("js", "jest_test_file",       re.compile(r"\.(test|spec)\.(t|j)sx?$")),
    ("js", "tests_dir_file",       re.compile(r"(^|/)__tests__/")),
    # Go
    ("go", "go_test_file",         re.compile(r"_test\.go$")),
    # Rust
    ("rust", "rust_test_module",   re.compile(r"(^|/)tests/[^/]+\.rs$")),
]


class TestPresenceExtractor:
    name = "test_presence"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            for ecosystem, indicator, pattern in _PATTERNS:
                if pattern.search(f.path):
                    yield EvidenceItem(
                        evidence_type="test_indicator",
                        locator_kind=LOCATOR_WHOLE_FILE,
                        locator={"path": f.path},
                        extracted_value={
                            "ecosystem": ecosystem,
                            "indicator": indicator,
                        },
                    )
                    break  # one indicator per file is enough
