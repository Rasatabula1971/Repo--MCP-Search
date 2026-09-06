"""
Model provider abstraction.

A ModelProvider is anything that can turn a prompt into a raw string
response. Deliberately dumb: no schema knowledge, no retry logic. Both
of those live at layers above.

Errors follow the same three-way split as connectors:
    TransientProviderError  — retry with backoff (5xx, rate limit)
    InputProviderError      — deterministic failure (400)
    BlockedProviderError    — auth/policy (401/403)

The workflow's retry classifier consumes these directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderResponse:
    """What a provider returns on a successful transport-level call. The
    response text is opaque — schema validation happens downstream."""
    raw_text: str
    latency_ms: int
    tokens_input: int | None = None
    tokens_output: int | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """
    Every provider — Gemini today, Groq/Cerebras/local Hermes tomorrow —
    exposes exactly this shape.
    """

    name: str

    def invoke(
        self,
        model_id: str,
        prompt: str,
    ) -> ProviderResponse: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base. Never raised directly."""


class TransientProviderError(ProviderError):
    """Retry with backoff — network flap, 5xx, rate limit."""


class InputProviderError(ProviderError):
    """400s and shape mismatches — retry won't help."""


class BlockedProviderError(ProviderError):
    """401/403 — human/config attention needed."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ProviderRegistry:
    """
    In-process registry mapping provider name to instance. Populated
    from providers.yaml at boot; also accepts programmatic registration
    for tests.

    Providers are configuration: no code path outside this registry and
    core/judgment/config.py names a provider by string.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(
                f"provider {provider.name!r} already registered"
            )
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise KeyError(
                f"no provider registered for {name!r}"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._providers.keys())
