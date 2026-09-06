"""
Providers config loader.

Reads config/providers.yaml, resolves it against the model_provider and
model_profile tables (upsert), and returns a ProviderRegistry populated
with instantiated provider objects.

This is the one place that names concrete provider classes. Everything
else works against the ModelProvider Protocol. That's what makes the
"no provider or model name in a code path" non-negotiable actually
enforceable — anyone reaching for a provider by name goes through here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.judgment.gemini import GeminiProvider
from core.judgment.providers import ModelProvider, ProviderRegistry


# provider_name -> class. New provider types are added here (and only
# here — everywhere else goes via config).
_PROVIDER_CLASSES: dict[str, type[ModelProvider]] = {
    "gemini": GeminiProvider,
    # Future: "openrouter": OpenRouterProvider, "ollama": OllamaProvider, ...
}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    tier: str
    base_url: str
    api_key_env: str
    data_retention_posture: str
    models: list[dict]
    metadata: dict


def load_config(path: str | Path) -> list[ProviderConfig]:
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or "providers" not in data:
        raise ValueError("providers.yaml must be an object with a 'providers' key")
    out: list[ProviderConfig] = []
    for entry in data["providers"]:
        out.append(ProviderConfig(
            name=entry["name"],
            tier=entry["tier"],
            base_url=entry["base_url"],
            api_key_env=entry["api_key_env"],
            data_retention_posture=entry["data_retention_posture"],
            models=entry.get("models", []) or [],
            metadata=entry.get("metadata", {}) or {},
        ))
    return out


def upsert_and_build_registry(
    conn,
    path: str | Path,
    *,
    provider_classes: dict[str, type[ModelProvider]] | None = None,
) -> ProviderRegistry:
    """
    Read yaml, upsert into model_provider + model_profile tables, build
    a runtime registry with instantiated provider objects.

    provider_classes lets tests inject fakes without touching the module
    globals — same trick as ConnectorRegistry.
    """
    classes = provider_classes if provider_classes is not None else _PROVIDER_CLASSES
    configs = load_config(path)

    registry = ProviderRegistry()
    with conn.cursor() as cur:
        for cfg in configs:
            cur.execute(
                """
                INSERT INTO model_provider
                  (name, tier, base_url, api_key_env,
                   data_retention_posture, metadata)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (name) DO UPDATE SET
                  tier = EXCLUDED.tier,
                  base_url = EXCLUDED.base_url,
                  api_key_env = EXCLUDED.api_key_env,
                  data_retention_posture = EXCLUDED.data_retention_posture,
                  metadata = EXCLUDED.metadata
                RETURNING id
                """,
                (cfg.name, cfg.tier, cfg.base_url, cfg.api_key_env,
                 cfg.data_retention_posture, json.dumps(cfg.metadata)),
            )
            provider_id = cur.fetchone()[0]

            for model in cfg.models:
                cur.execute(
                    """
                    INSERT INTO model_profile
                      (provider_id, model_id, tool_calling, enabled, metadata)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (provider_id, model_id) DO UPDATE SET
                      tool_calling = EXCLUDED.tool_calling,
                      enabled = EXCLUDED.enabled,
                      metadata = EXCLUDED.metadata
                    """,
                    (
                        provider_id, model["id"],
                        model.get("tool_calling", False),
                        model.get("enabled", True),
                        json.dumps({
                            k: v for k, v in model.items()
                            if k not in ("id", "tool_calling", "enabled")
                        }),
                    ),
                )

            klass = classes.get(cfg.name)
            if klass is None:
                # A yaml entry with no registered class isn't fatal — the
                # row is in the DB, we just can't instantiate it. The
                # orchestrator will fail loudly if asked to use it.
                continue
            registry.register(klass(
                name=cfg.name,
                api_key_env=cfg.api_key_env,
                base_url=cfg.base_url,
            ))

    return registry


def resolve_provider_and_model_ids(
    conn, provider_name: str, model_id: str
) -> tuple[str, str]:
    """Look up the DB ids for a (provider_name, model_id) pair."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mp.id, prof.id
            FROM model_provider mp
            JOIN model_profile prof ON prof.provider_id = mp.id
            WHERE mp.name = %s AND prof.model_id = %s
            """,
            (provider_name, model_id),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(
                f"provider={provider_name!r} model={model_id!r} not in DB"
            )
        return row[0], row[1]
