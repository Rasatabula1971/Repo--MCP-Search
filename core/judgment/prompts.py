"""
Prompt registry.

Prompts are named + versioned. Bumping a prompt's version means
"downstream evidence based on the old version is not comparable to
evidence based on the new version" — the workflow uses the version
to decide whether prior judgments still apply.

Each prompt exposes:
    name:     stable identifier
    version:  monotonic integer
    render(context) -> str

The rendered string is what gets hashed and sent to the provider. If
you need to change the wording, bump the version — never edit in place.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    render: Callable[[dict], str]

    def render_and_hash(self, context: dict) -> tuple[str, str]:
        text = self.render(context)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, h


# ---------------------------------------------------------------------------
# capability_summary v1
# ---------------------------------------------------------------------------

def _render_capability_summary(context: dict) -> str:
    """
    Rendered prompt for the initial capability summary judgment. Context
    keys expected:
        asset_display_name:  e.g. 'psf/requests'
        revision_key:        e.g. 'abc123'
        evidence_snippets:   list of {id, evidence_type, extracted_value}
    """
    lines = [
        "You are analysing a source repository to summarise its capabilities.",
        "",
        f"Asset: {context['asset_display_name']}",
        f"Revision: {context['revision_key']}",
        "",
        "Evidence collected by static analysis:",
    ]
    for e in context.get("evidence_snippets", []):
        lines.append(
            f"  - [{e['id']}] {e['evidence_type']}: {e['extracted_value']}"
        )
    lines += [
        "",
        "Respond with a JSON object containing exactly these fields:",
        '  verdict:         a short string, one of "well_scoped" | "narrow" |',
        '                    "sprawling" | "unclear"',
        "  criteria_scores: object mapping dimension name to float in [0,1].",
        '                    Must include at least "maturity" and "clarity".',
        "  self_confidence: float in [0,1] — your own confidence in this reading.",
        "  evidence_refs:   list of evidence ids (from above) that back the",
        "                    verdict. Use the ids in square brackets.",
        "",
        "Return ONLY the JSON object. No prose, no code fences.",
    ]
    return "\n".join(lines)


PROMPTS: dict[str, Prompt] = {
    "capability_summary": Prompt(
        name="capability_summary",
        version=1,
        render=_render_capability_summary,
    ),
}


def get(name: str) -> Prompt:
    try:
        return PROMPTS[name]
    except KeyError:
        raise KeyError(f"no prompt registered: {name!r}") from None
