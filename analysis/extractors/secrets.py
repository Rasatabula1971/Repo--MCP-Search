"""
Secret indicator extractor.

Regex-based observation of strings that LOOK like credentials. We emit
'secret_indicator' evidence with a redacted preview — never the raw
string. The observation is 'we found something that matches pattern X
at path:line'; whether it's an actual secret vs test fixture vs example
is the interpretation layer's problem, not ours.

Patterns are deliberately conservative — false positives here become
noise the reviewer has to filter. Better to miss some than flood the
gate layer with garbage.

We do NOT:
  - Fetch anything (no HTTP calls to validate keys)
  - Store the raw match (only redacted preview)
  - Rank severity (interpretation)
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_FILE_RANGE,
    EvidenceItem,
    SourceFile,
)


# (pattern_name, compiled_regex, min_len_to_report)
# Patterns intentionally strict — anchored to the vendor's fixed prefix
# where possible. Generic entropy detection is out of scope for Step 6.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key",   re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key",   re.compile(rb"\baws[_-]?secret[_-]?access[_-]?key['\"= ]+[A-Za-z0-9/+=]{40}\b", re.IGNORECASE)),
    ("github_pat",       re.compile(rb"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_fine_pat",  re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{80,}\b")),
    ("openai_key",       re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b")),
    ("google_api_key",   re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token",      re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_secret",    re.compile(rb"\bsk_live_[A-Za-z0-9]{24,}\b")),
    ("private_key_pem",  re.compile(rb"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
]


# Skip binary-looking files up front. We only scan text.
_TEXT_LIKE_LANGS = {
    "python", "javascript", "typescript", "go", "rust", "java", "ruby",
    "shell", "markdown", "yaml", "json", "toml", "sql", "html", "css",
}


class SecretIndicatorExtractor:
    name = "secret_indicators"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if not _is_text_scan_candidate(f):
                continue
            for name, pattern in _PATTERNS:
                for match in pattern.finditer(f.content):
                    lineno = _line_number(f.content, match.start())
                    yield EvidenceItem(
                        evidence_type="secret_indicator",
                        locator_kind=LOCATOR_FILE_RANGE,
                        locator={
                            "path": f.path,
                            "start_line": lineno,
                            "end_line": lineno,
                        },
                        extracted_value={
                            "pattern_name": name,
                            "redacted_preview": _redact(match.group(0)),
                        },
                    )


def _is_text_scan_candidate(f: SourceFile) -> bool:
    if f.language in _TEXT_LIKE_LANGS:
        return True
    # Small files without a detected language: sniff for text.
    if len(f.content) > 2 * 1024 * 1024:      # >2 MB, skip
        return False
    # If more than 1% of bytes are NULs, treat as binary.
    if b"\x00" in f.content[:8192]:
        return False
    return True


def _line_number(content: bytes, byte_offset: int) -> int:
    """1-indexed line number of the given byte offset."""
    return content.count(b"\n", 0, byte_offset) + 1


def _redact(match_bytes: bytes) -> str:
    """
    Never emit the raw match. Keep 4 leading chars for pattern context,
    hide the rest with * of the same length.
    """
    s = match_bytes.decode("utf-8", errors="replace")
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 4)
