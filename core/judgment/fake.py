"""
FakeProvider — deterministic ModelProvider for tests.

Script a queue of responses; each invoke() pops the next one. If the
scripted item is an exception, invoke() raises it. This is what lets
tests exercise:
  - valid + schema-valid responses (happy path)
  - schema-invalid responses (the Step 7 done-when)
  - transport-level errors of each retry class
"""
from __future__ import annotations

from collections import deque

from core.judgment.providers import ProviderResponse


class FakeProvider:
    """Implements ModelProvider Protocol for tests."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self._scripted: deque = deque()
        self.calls: list[tuple[str, str]] = []   # (model_id, prompt)

    def script_response(
        self,
        raw_text: str,
        *,
        latency_ms: int = 5,
        tokens_input: int | None = 10,
        tokens_output: int | None = 10,
    ) -> None:
        self._scripted.append(ProviderResponse(
            raw_text=raw_text,
            latency_ms=latency_ms,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        ))

    def script_error(self, exc: Exception) -> None:
        self._scripted.append(exc)

    def invoke(self, model_id: str, prompt: str) -> ProviderResponse:
        self.calls.append((model_id, prompt))
        if not self._scripted:
            raise RuntimeError(
                "FakeProvider: no scripted response for this call"
            )
        item = self._scripted.popleft()
        if isinstance(item, Exception):
            raise item
        return item
