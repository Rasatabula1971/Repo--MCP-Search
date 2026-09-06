"""
Gemini provider.

Direct REST calls to generativelanguage.googleapis.com. No use of the
google-generativeai SDK — one less external dep, and it keeps the
core.scoring import-linter contract simple (forbidding "google" covers
everything).

Uses API key auth (Google's free tier) via ?key=... query param, which
is Google's documented pattern for API-key clients. When we add OAuth
later for higher tiers it's a separate provider class.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from core.judgment.providers import (
    BlockedProviderError,
    InputProviderError,
    ProviderResponse,
    TransientProviderError,
)


DEFAULT_TIMEOUT = 60.0     # LLM calls are slow


class GeminiProvider:
    """Implements ModelProvider Protocol for Google Gemini."""

    def __init__(
        self,
        name: str = "gemini",
        api_key_env: str = "GEMINI_API_KEY",
        base_url: str = "https://generativelanguage.googleapis.com",
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def _api_key(self) -> str:
        key = os.environ.get(self._api_key_env, "")
        if not key:
            # No key — treat as blocked, not transient. Retry won't help
            # until someone sets the env var.
            raise BlockedProviderError(
                f"provider {self.name!r}: {self._api_key_env} is unset"
            )
        return key

    def invoke(self, model_id: str, prompt: str) -> ProviderResponse:
        url = (
            f"{self._base_url}/v1beta/models/{model_id}:generateContent"
        )
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            # Force JSON output where the model supports it. This is
            # advisory to the model; we still validate downstream.
            "generationConfig": {
                "response_mime_type": "application/json",
                "temperature": 0.0,      # deterministic for the same input
            },
        }
        started = time.monotonic()
        try:
            resp = self._client.post(
                url,
                params={"key": self._api_key()},
                headers={"Content-Type": "application/json"},
                content=json.dumps(payload),
            )
        except httpx.TransportError as exc:
            raise TransientProviderError(
                f"transport error calling {self.name}: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        _raise_for_status(resp, context=f"{self.name}:{model_id}")

        # Extract the text and token counts.
        body = resp.json()
        text, tin, tout = _extract_gemini_payload(body)
        return ProviderResponse(
            raw_text=text,
            latency_ms=latency_ms,
            tokens_input=tin,
            tokens_output=tout,
        )


def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
    if resp.is_success:
        return
    status = resp.status_code
    if status == 429 or (500 <= status < 600):
        raise TransientProviderError(
            f"{context}: status {status} — retry"
        )
    if status in (401, 403):
        raise BlockedProviderError(f"{context}: status {status}")
    # Everything else — treat as input error. Retrying won't help.
    raise InputProviderError(
        f"{context}: unexpected status {status}: {resp.text[:200]}"
    )


def _extract_gemini_payload(body: dict) -> tuple[str, int | None, int | None]:
    """
    Parse Gemini's response envelope. Returns (text, tokens_in, tokens_out).

    Response shape (simplified):
      {
        "candidates": [{"content": {"parts": [{"text": "..."}]}}],
        "usageMetadata": {"promptTokenCount": N, "candidatesTokenCount": M}
      }

    A malformed envelope is treated as an input error rather than
    something we retry — if Gemini is returning garbage envelopes,
    hammering it more won't help. But this is envelope-level; schema
    validation of the JSON *inside* the text happens elsewhere.
    """
    try:
        candidates = body.get("candidates") or []
        if not candidates:
            raise InputProviderError(
                f"gemini: empty candidates array; full body: {body}"
            )
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        text = "".join(text_parts)
        usage = body.get("usageMetadata", {}) or {}
        return (
            text,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise InputProviderError(
            f"gemini: malformed response envelope: {exc}"
        ) from exc
