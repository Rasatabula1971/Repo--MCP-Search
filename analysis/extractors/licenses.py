"""
License extractor.

Looks for LICENSE-like files at the repo root and tries to identify the
SPDX id from content. Uses a small pattern set covering the common
open-source licenses — MIT, Apache-2.0, BSD-2, BSD-3, ISC, GPL-2/3,
LGPL-2.1/3, MPL-2.0, Unlicense.

Confidence:
  high    — a distinctive phrase from the canonical text was found
  low     — the header mentions the license by name only
  unknown — a LICENSE file exists but we couldn't identify it

Even at 'unknown' we still emit evidence, because "there is a LICENSE
file" is itself evidence the gates layer cares about.
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from analysis.evidence import (
    LOCATOR_WHOLE_FILE,
    EvidenceItem,
    SourceFile,
)


_LICENSE_FILENAME_RE = re.compile(
    r"^(LICEN[SC]E|COPYING|COPYRIGHT)([._-].*)?$",
    re.IGNORECASE,
)


# (spdx_id, distinctive_phrase, confidence)
# Ordered — first match wins. Distinctive phrases chosen to avoid
# false matches (e.g. every license mentions "license", so we use
# phrases unique to each).
_SIGNATURES: list[tuple[str, str, str]] = [
    ("Apache-2.0",
     "Licensed under the Apache License, Version 2.0", "high"),
    ("Apache-2.0",
     "www.apache.org/licenses/LICENSE-2.0", "high"),
    ("MIT",
     "Permission is hereby granted, free of charge, to any person obtaining a copy",
     "high"),
    ("BSD-3-Clause",
     "Neither the name of", "high"),
    ("BSD-2-Clause",
     "Redistribution and use in source and binary forms",
     "low"),   # also present in BSD-3; kept low so BSD-3 wins first
    ("ISC",
     "Permission to use, copy, modify, and/or distribute", "high"),
    ("GPL-3.0",
     "GNU GENERAL PUBLIC LICENSE\n                       Version 3",
     "high"),
    ("GPL-3.0",
     "GNU GENERAL PUBLIC LICENSE", "low"),
    ("GPL-2.0",
     "GNU GENERAL PUBLIC LICENSE\n                       Version 2",
     "high"),
    ("LGPL-3.0",
     "GNU LESSER GENERAL PUBLIC LICENSE", "low"),
    ("MPL-2.0",
     "Mozilla Public License Version 2.0", "high"),
    ("Unlicense",
     "This is free and unencumbered software released into the public domain",
     "high"),
]


class LicenseExtractor:
    name = "license"

    def extract(self, files: Iterable[SourceFile]) -> Iterator[EvidenceItem]:
        for f in files:
            if not _is_license_file(f.path):
                continue
            spdx, confidence = _identify(f.content)
            yield EvidenceItem(
                evidence_type="license",
                locator_kind=LOCATOR_WHOLE_FILE,
                locator={"path": f.path},
                extracted_value={
                    "spdx_id": spdx,
                    "confidence": confidence,
                    "size_bytes": len(f.content),
                },
            )


def _is_license_file(path: str) -> bool:
    # Only at repo root or one level deep. A LICENSE inside vendored code
    # shouldn't be misread as the whole repo's license.
    parts = path.split("/")
    if len(parts) > 2:
        return False
    filename = parts[-1]
    return bool(_LICENSE_FILENAME_RE.match(filename))


def _identify(content: bytes) -> tuple[str, str]:
    text = content.decode("utf-8", errors="replace")
    for spdx, phrase, confidence in _SIGNATURES:
        if phrase in text:
            return spdx, confidence
    return "unknown", "unknown"
