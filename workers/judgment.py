"""
Judgment orchestrator — Step 7.

One entry point: run_judgment(). It writes:

  1. A judgment_request row (prompt + version + hash + evidence refs)
  2. Exactly ONE provider call
  3. Either:
     - judgment_response with schema_valid=true and parsed fields, OR
     - judgment_response with schema_valid=false, raw preserved, and NO
       retry to the model, OR
     - judgment_error and re-raise (workflow layer decides retry)

The done-when: a malformed response produces a schema_valid=false row
and is NOT followed by a second model call to "figure out what went wrong."
The row is provider-reliability evidence — its accumulation over time
is what the tier system in Step 15+ acts on.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from core.judgment.config import resolve_provider_and_model_ids
from core.judgment.prompts import get as get_prompt
from core.judgment.providers import (
    BlockedProviderError,
    InputProviderError,
    ProviderRegistry,
    ProviderResponse,
    TransientProviderError,
)
from core.judgment.schema import Judgment, SchemaError, parse
from core.workflow.engine import ErrorClass


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JudgmentOutcome:
    request_id: uuid.UUID
    response_id: uuid.UUID | None
    schema_valid: bool
    judgment: Judgment | None
    latency_ms: int
    schema_error: str | None = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_judgment(
    conn: psycopg.Connection,
    *,
    registry: ProviderRegistry,
    source_revision_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    prompt_name: str,
    context: dict,
    provider_name: str,
    model_id: str,
    input_evidence_ids: list[uuid.UUID] | None = None,
) -> JudgmentOutcome:
    """
    Run one judgment: render prompt, call provider, validate, persist.
    Caller manages the transaction — this function commits after each
    meaningful write so a crash between provider call and persist doesn't
    lose the response.

    provider_name is passed in from the caller — the orchestrator itself
    doesn't hardcode a choice. This is what "providers are configuration"
    looks like at the call site: policy decides which provider, this
    function just uses it.
    """
    prompt = get_prompt(prompt_name)
    rendered, prompt_hash = prompt.render_and_hash(context)

    provider_db_id, profile_db_id = resolve_provider_and_model_ids(
        conn, provider_name, model_id
    )

    request_id = _create_request(
        conn,
        source_revision_id=source_revision_id,
        analysis_run_id=analysis_run_id,
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        prompt_hash=prompt_hash,
        input_evidence_ids=input_evidence_ids or [],
    )
    conn.commit()

    provider = registry.get(provider_name)

    # ONE call. Not two, not two-with-a-retry-for-parse-failure.
    try:
        resp = provider.invoke(model_id, rendered)
    except (TransientProviderError, InputProviderError, BlockedProviderError) as exc:
        _record_provider_error(
            conn, request_id=request_id,
            provider_db_id=provider_db_id, exc=exc,
        )
        conn.commit()
        raise  # workflow layer classifies and retries per its policy

    # Validate deterministically. On failure: persist schema_valid=false
    # and RETURN — no second model call.
    try:
        judgment = parse(resp.raw_text)
        schema_error: str | None = None
        response_id = _persist_valid(
            conn,
            request_id=request_id,
            provider_db_id=provider_db_id,
            profile_db_id=profile_db_id,
            judgment=judgment,
            resp=resp,
        )
        conn.commit()
        return JudgmentOutcome(
            request_id=request_id,
            response_id=response_id,
            schema_valid=True,
            judgment=judgment,
            latency_ms=resp.latency_ms,
        )
    except SchemaError as exc:
        response_id = _persist_invalid(
            conn,
            request_id=request_id,
            provider_db_id=provider_db_id,
            profile_db_id=profile_db_id,
            resp=resp,
            schema_error=str(exc),
        )
        conn.commit()
        return JudgmentOutcome(
            request_id=request_id,
            response_id=response_id,
            schema_valid=False,
            judgment=None,
            latency_ms=resp.latency_ms,
            schema_error=str(exc),
        )


# ---------------------------------------------------------------------------
# Retry classification helper — exposed for workflow layer callers
# ---------------------------------------------------------------------------

def classify_provider_error(exc: BaseException) -> ErrorClass:
    if isinstance(exc, TransientProviderError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, BlockedProviderError):
        return ErrorClass.POLICY
    if isinstance(exc, InputProviderError):
        return ErrorClass.INPUT
    return ErrorClass.TRANSIENT


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------

def _create_request(
    conn,
    *,
    source_revision_id: uuid.UUID,
    analysis_run_id: uuid.UUID | None,
    prompt_name: str,
    prompt_version: int,
    prompt_hash: str,
    input_evidence_ids: list[uuid.UUID],
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO judgment_request
              (source_revision_id, analysis_run_id, prompt_name,
               prompt_version, prompt_hash, input_evidence_ids)
            VALUES (%s, %s, %s, %s, %s, %s::uuid[])
            RETURNING id
            """,
            (
                str(source_revision_id),
                str(analysis_run_id) if analysis_run_id else None,
                prompt_name,
                prompt_version,
                prompt_hash,
                [str(x) for x in input_evidence_ids],
            ),
        )
        return cur.fetchone()[0]


def _persist_valid(
    conn,
    *,
    request_id: uuid.UUID,
    provider_db_id: uuid.UUID,
    profile_db_id: uuid.UUID,
    judgment: Judgment,
    resp: ProviderResponse,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO judgment_response
              (judgment_request_id, provider_id, model_profile_id,
               verdict, criteria_scores, self_confidence, evidence_refs,
               schema_valid, raw_response,
               latency_ms, tokens_input, tokens_output)
            VALUES (%s, %s, %s,
                    %s, %s::jsonb, %s, %s::jsonb,
                    true, %s,
                    %s, %s, %s)
            RETURNING id
            """,
            (
                str(request_id), str(provider_db_id), str(profile_db_id),
                judgment.verdict,
                json.dumps(judgment.criteria_scores),
                judgment.self_confidence,
                json.dumps(judgment.evidence_refs),
                resp.raw_text,
                resp.latency_ms, resp.tokens_input, resp.tokens_output,
            ),
        )
        return cur.fetchone()[0]


def _persist_invalid(
    conn,
    *,
    request_id: uuid.UUID,
    provider_db_id: uuid.UUID,
    profile_db_id: uuid.UUID,
    resp: ProviderResponse,
    schema_error: str,
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO judgment_response
              (judgment_request_id, provider_id, model_profile_id,
               schema_valid, schema_error, raw_response,
               latency_ms, tokens_input, tokens_output)
            VALUES (%s, %s, %s,
                    false, %s, %s,
                    %s, %s, %s)
            RETURNING id
            """,
            (
                str(request_id), str(provider_db_id), str(profile_db_id),
                schema_error, resp.raw_text,
                resp.latency_ms, resp.tokens_input, resp.tokens_output,
            ),
        )
        return cur.fetchone()[0]


def _record_provider_error(
    conn,
    *,
    request_id: uuid.UUID,
    provider_db_id: uuid.UUID | None,
    exc: BaseException,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO judgment_error
              (judgment_request_id, provider_id, error_class, error_detail)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                str(request_id),
                str(provider_db_id) if provider_db_id else None,
                classify_provider_error(exc).value,
                json.dumps({"message": str(exc), "type": type(exc).__name__}),
            ),
        )
