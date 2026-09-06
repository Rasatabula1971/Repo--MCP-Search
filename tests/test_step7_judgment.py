"""
Step 7 done-when:
  "A deliberately malformed provider response is rejected without a
   retry to the model, and appears in the database as
   provider-reliability evidence."

Test coverage:
  1. Judgment schema validation — accepts good, rejects bad
     (missing fields, wrong types, out-of-range scores, bad UUIDs).
  2. Provider Protocol — Fake and Gemini both satisfy it.
  3. Gemini HTTP status → error class mapping (same shape as GitHub).
  4. Providers.yaml loader upserts model_provider + model_profile rows,
     builds a registry with instantiated providers.
  5. Orchestrator writes judgment_request with prompt version + hash.
  6. Valid response → schema_valid=true row with parsed fields.
  7. Malformed response → schema_valid=false row, raw preserved,
     exactly ONE provider call (the done-when).
  8. Transient/blocked/input errors → judgment_error row, exception
     re-raised (workflow decides retry).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest

from connectors.base import ConnectorRegistry
from connectors.fake import FakeConnector
from core.judgment.config import (
    load_config,
    resolve_provider_and_model_ids,
    upsert_and_build_registry,
)
from core.judgment.fake import FakeProvider
from core.judgment.gemini import GeminiProvider
from core.judgment.providers import (
    BlockedProviderError,
    InputProviderError,
    ModelProvider,
    ProviderRegistry,
    TransientProviderError,
)
from core.judgment.schema import Judgment, SchemaError, parse
from core.workflow.engine import ErrorClass
from workers.analysis import run_analysis
from workers.ingestion import ingest_revision
from workers.judgment import (
    classify_provider_error,
    run_judgment,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _valid_payload(**overrides):
    body = {
        "verdict": "well_scoped",
        "criteria_scores": {"maturity": 0.7, "clarity": 0.9},
        "self_confidence": 0.8,
        "evidence_refs": [str(uuid.uuid4()), str(uuid.uuid4())],
    }
    body.update(overrides)
    return json.dumps(body)


def test_schema_accepts_wellformed_payload():
    j = parse(_valid_payload())
    assert isinstance(j, Judgment)
    assert j.verdict == "well_scoped"
    assert j.criteria_scores == {"maturity": 0.7, "clarity": 0.9}
    assert j.self_confidence == 0.8
    assert len(j.evidence_refs) == 2


def test_schema_rejects_non_json():
    with pytest.raises(SchemaError):
        parse("not json at all")


def test_schema_rejects_json_list_at_top_level():
    with pytest.raises(SchemaError):
        parse("[1, 2, 3]")


def test_schema_rejects_missing_fields():
    for missing in ("verdict", "criteria_scores", "self_confidence", "evidence_refs"):
        body = json.loads(_valid_payload())
        del body[missing]
        with pytest.raises(SchemaError, match="missing required fields"):
            parse(json.dumps(body))


def test_schema_rejects_empty_verdict():
    with pytest.raises(SchemaError):
        parse(_valid_payload(verdict=""))


def test_schema_rejects_score_out_of_range():
    with pytest.raises(SchemaError):
        parse(_valid_payload(self_confidence=1.5))
    with pytest.raises(SchemaError):
        parse(_valid_payload(self_confidence=-0.1))


def test_schema_rejects_string_scores():
    """A '0.8' string is NOT a score. We want the reliability metric
    to surface providers that return the wrong types."""
    with pytest.raises(SchemaError):
        parse(_valid_payload(self_confidence="0.8"))


def test_schema_rejects_bool_scores():
    """True is technically int(1) in Python but semantically not a score."""
    with pytest.raises(SchemaError):
        parse(_valid_payload(self_confidence=True))


def test_schema_rejects_bad_uuid_in_evidence_refs():
    with pytest.raises(SchemaError, match="not a valid UUID"):
        parse(_valid_payload(evidence_refs=["not-a-uuid"]))


def test_schema_rejects_empty_criteria_scores():
    with pytest.raises(SchemaError):
        parse(_valid_payload(criteria_scores={}))


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------

def test_fake_provider_satisfies_protocol():
    assert isinstance(FakeProvider(), ModelProvider)


def test_gemini_provider_satisfies_protocol():
    assert isinstance(GeminiProvider(), ModelProvider)


def test_provider_registry_rejects_duplicate():
    reg = ProviderRegistry()
    reg.register(FakeProvider(name="a"))
    with pytest.raises(ValueError):
        reg.register(FakeProvider(name="a"))


def test_provider_registry_missing_raises():
    reg = ProviderRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


# ---------------------------------------------------------------------------
# Gemini HTTP status mapping
# ---------------------------------------------------------------------------

def _gemini_with_transport(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return GeminiProvider(client=client)


def test_gemini_401_becomes_blocked():
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-test"
    def handler(req):
        return httpx.Response(401, json={"error": "bad key"})
    g = _gemini_with_transport(handler)
    with pytest.raises(BlockedProviderError):
        g.invoke("gemini-2.5-flash", "hello")


def test_gemini_500_becomes_transient():
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-test"
    def handler(req):
        return httpx.Response(500, text="internal error")
    g = _gemini_with_transport(handler)
    with pytest.raises(TransientProviderError):
        g.invoke("gemini-2.5-flash", "hello")


def test_gemini_429_becomes_transient():
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-test"
    def handler(req):
        return httpx.Response(429, text="rate limited")
    g = _gemini_with_transport(handler)
    with pytest.raises(TransientProviderError):
        g.invoke("gemini-2.5-flash", "hello")


def test_gemini_missing_api_key_is_blocked():
    import os
    os.environ.pop("GEMINI_API_KEY", None)
    def handler(req):
        raise AssertionError("should never reach the wire")
    g = _gemini_with_transport(handler)
    with pytest.raises(BlockedProviderError, match="GEMINI_API_KEY is unset"):
        g.invoke("gemini-2.5-flash", "hello")


def test_gemini_extracts_text_and_tokens_from_envelope():
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-test"
    def handler(req):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "hello world"}]}}],
            "usageMetadata": {
                "promptTokenCount": 42,
                "candidatesTokenCount": 8,
            },
        })
    g = _gemini_with_transport(handler)
    r = g.invoke("gemini-2.5-flash", "prompt")
    assert r.raw_text == "hello world"
    assert r.tokens_input == 42
    assert r.tokens_output == 8


def test_gemini_malformed_envelope_is_input_error():
    """Empty candidates array from the API — retry won't help."""
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-test"
    def handler(req):
        return httpx.Response(200, json={"candidates": []})
    g = _gemini_with_transport(handler)
    with pytest.raises(InputProviderError):
        g.invoke("gemini-2.5-flash", "prompt")


# ---------------------------------------------------------------------------
# providers.yaml loading + DB upsert
# ---------------------------------------------------------------------------

def test_load_config_parses_stock_yaml():
    cfg = load_config(Path(__file__).parent.parent / "config" / "providers.yaml")
    assert len(cfg) == 1
    assert cfg[0].name == "gemini"
    assert cfg[0].tier == "free_primary"
    assert cfg[0].api_key_env == "GEMINI_API_KEY"
    assert any(m["id"] == "gemini-2.5-flash" for m in cfg[0].models)


def test_upsert_creates_provider_and_profile_rows(conn):
    reg = upsert_and_build_registry(
        conn,
        Path(__file__).parent.parent / "config" / "providers.yaml",
    )
    conn.commit()
    assert reg.names() == ["gemini"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, tier, api_key_env, data_retention_posture "
            "FROM model_provider WHERE name = 'gemini'"
        )
        row = cur.fetchone()
    assert row == ("gemini", "free_primary", "GEMINI_API_KEY", "trains_on_input")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT model_id, tool_calling FROM model_profile p "
            "JOIN model_provider pr ON pr.id = p.provider_id "
            "WHERE pr.name = 'gemini'"
        )
        rows = cur.fetchall()
    assert ("gemini-2.5-flash", True) in rows


def test_upsert_is_idempotent(conn):
    """Loading twice doesn't duplicate rows — the ON CONFLICT clause
    is doing its job."""
    path = Path(__file__).parent.parent / "config" / "providers.yaml"
    upsert_and_build_registry(conn, path)
    upsert_and_build_registry(conn, path)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM model_provider WHERE name = 'gemini'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT COUNT(*) FROM model_profile p "
            "JOIN model_provider pr ON pr.id = p.provider_id "
            "WHERE pr.name = 'gemini'"
        )
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Fixture: a revision with analysis run so judgments have real inputs
# ---------------------------------------------------------------------------

@pytest.fixture
def registry_and_snapshot(conn):
    """
    Ingest a fixture repo, run analysis, provision a FakeProvider under
    the 'gemini' name (so resolve_provider_and_model_ids works), and
    return everything callers need to run a judgment.
    """
    connectors = ConnectorRegistry()
    fake_conn = FakeConnector(name="fake")
    connectors.register(fake_conn)
    fake_conn.add_revision("acme/lib", "sha_1", {
        "pyproject.toml": b"[project]\ndependencies = ['httpx']\n",
        "src/core.py": b"def hello():\n    return 'hi'\n",
        "LICENSE": (
            b"MIT License\n\n"
            b"Permission is hereby granted, free of charge, to any "
            b"person obtaining a copy\n"
        ),
    })
    # Insert the source rows, ingest, analyse.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_provider (name, kind) "
            "VALUES ('fake', 'code_host') "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id"
        )
        provider_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_asset "
            "(provider_id, external_key, display_name, kind) "
            "VALUES (%s, 'acme/lib', 'acme/lib', 'repository') RETURNING id",
            (provider_id,),
        )
        asset_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO source_revision (source_asset_id, revision_key) "
            "VALUES (%s, 'sha_1') RETURNING id",
            (asset_id,),
        )
        rev_id = cur.fetchone()[0]
    conn.commit()
    ingest_revision(conn, registry=connectors, source_revision_id=rev_id)
    analysis = run_analysis(
        conn, registry=connectors, source_revision_id=rev_id
    )

    # Provision model_provider + model_profile rows for 'gemini' with a
    # FakeProvider instance in the registry under that name. The DB rows
    # let resolve_provider_and_model_ids succeed; the FakeProvider does
    # the actual invoke().
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_provider
              (name, tier, base_url, api_key_env, data_retention_posture)
            VALUES ('gemini', 'free_primary', 'https://example',
                    'GEMINI_API_KEY', 'trains_on_input') RETURNING id
            """,
        )
        gemini_db_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO model_profile (provider_id, model_id, tool_calling) "
            "VALUES (%s, 'gemini-2.5-flash', true) RETURNING id",
            (gemini_db_id,),
        )
    conn.commit()

    providers = ProviderRegistry()
    fake_provider = FakeProvider(name="gemini")
    providers.register(fake_provider)

    return {
        "connectors": connectors,
        "providers": providers,
        "fake_provider": fake_provider,
        "revision_id": rev_id,
        "analysis_run_id": analysis.analysis_run_id,
    }


def _judgment_context(display_name="acme/lib", revision_key="sha_1", refs=None):
    refs = refs or []
    return {
        "asset_display_name": display_name,
        "revision_key": revision_key,
        "evidence_snippets": [
            {"id": ref, "evidence_type": "dependency",
             "extracted_value": {"name": "httpx"}}
            for ref in refs
        ],
    }


# ---------------------------------------------------------------------------
# Orchestrator — happy path
# ---------------------------------------------------------------------------

def test_judgment_records_request_with_prompt_version_and_hash(
    conn, registry_and_snapshot
):
    rig = registry_and_snapshot
    rig["fake_provider"].script_response(_valid_payload())

    outcome = run_judgment(
        conn,
        registry=rig["providers"],
        source_revision_id=rig["revision_id"],
        analysis_run_id=rig["analysis_run_id"],
        prompt_name="capability_summary",
        context=_judgment_context(),
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )
    assert outcome.schema_valid is True
    assert outcome.judgment is not None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT prompt_name, prompt_version, LENGTH(prompt_hash) "
            "FROM judgment_request WHERE id = %s",
            (str(outcome.request_id),),
        )
        row = cur.fetchone()
    assert row == ("capability_summary", 1, 64)   # SHA256 hex is 64 chars


def test_judgment_records_evidence_refs_used(conn, registry_and_snapshot):
    rig = registry_and_snapshot
    ev1, ev2 = uuid.uuid4(), uuid.uuid4()
    rig["fake_provider"].script_response(_valid_payload())

    outcome = run_judgment(
        conn,
        registry=rig["providers"],
        source_revision_id=rig["revision_id"],
        analysis_run_id=rig["analysis_run_id"],
        prompt_name="capability_summary",
        context=_judgment_context(refs=[str(ev1), str(ev2)]),
        provider_name="gemini",
        model_id="gemini-2.5-flash",
        input_evidence_ids=[ev1, ev2],
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT input_evidence_ids FROM judgment_request WHERE id = %s",
            (str(outcome.request_id),),
        )
        ids = cur.fetchone()[0]
    assert set(str(x) for x in ids) == {str(ev1), str(ev2)}


def test_valid_response_persists_parsed_fields(conn, registry_and_snapshot):
    rig = registry_and_snapshot
    rig["fake_provider"].script_response(_valid_payload(
        verdict="narrow",
        criteria_scores={"maturity": 0.42, "clarity": 0.9},
        self_confidence=0.55,
    ))
    outcome = run_judgment(
        conn,
        registry=rig["providers"],
        source_revision_id=rig["revision_id"],
        analysis_run_id=rig["analysis_run_id"],
        prompt_name="capability_summary",
        context=_judgment_context(),
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, criteria_scores, self_confidence, "
            "schema_valid, schema_error "
            "FROM judgment_response WHERE id = %s",
            (str(outcome.response_id),),
        )
        row = cur.fetchone()
    verdict, scores, confidence, valid, err = row
    assert verdict == "narrow"
    assert scores == {"maturity": 0.42, "clarity": 0.9}
    assert float(confidence) == 0.55
    assert valid is True
    assert err is None


# ---------------------------------------------------------------------------
# The Step 7 done-when
# ---------------------------------------------------------------------------

def test_malformed_response_persists_invalid_row_and_does_not_retry(
    conn, registry_and_snapshot
):
    """
    A malformed provider response is rejected DETERMINISTICALLY:
      - one provider call, not two
      - one judgment_response row, schema_valid=false, raw preserved
      - schema_error captures what went wrong
    This is the row Step 15's tiering acts on: enough of these from a
    provider drops it a tier.
    """
    rig = registry_and_snapshot
    rig["fake_provider"].script_response('this is not json at all')

    outcome = run_judgment(
        conn,
        registry=rig["providers"],
        source_revision_id=rig["revision_id"],
        analysis_run_id=rig["analysis_run_id"],
        prompt_name="capability_summary",
        context=_judgment_context(),
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )

    # Exactly ONE model call.
    assert len(rig["fake_provider"].calls) == 1

    # Outcome flags it invalid.
    assert outcome.schema_valid is False
    assert outcome.judgment is None
    assert "not valid JSON" in outcome.schema_error

    # DB row exists with the raw preserved and parsed fields NULL.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_valid, schema_error, raw_response, "
            "verdict, criteria_scores, self_confidence "
            "FROM judgment_response WHERE id = %s",
            (str(outcome.response_id),),
        )
        row = cur.fetchone()
    valid, err, raw, verdict, scores, conf = row
    assert valid is False
    assert "not valid JSON" in err
    assert raw == 'this is not json at all'
    assert verdict is None
    assert scores is None
    assert conf is None


def test_missing_field_in_response_persists_invalid_no_retry(
    conn, registry_and_snapshot
):
    """Well-formed JSON but missing a required field is still a Step 7
    schema failure — same handling: persist, don't retry."""
    rig = registry_and_snapshot
    body = json.loads(_valid_payload())
    del body["self_confidence"]
    rig["fake_provider"].script_response(json.dumps(body))

    outcome = run_judgment(
        conn,
        registry=rig["providers"],
        source_revision_id=rig["revision_id"],
        analysis_run_id=rig["analysis_run_id"],
        prompt_name="capability_summary",
        context=_judgment_context(),
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )
    assert outcome.schema_valid is False
    assert len(rig["fake_provider"].calls) == 1
    assert "self_confidence" in outcome.schema_error


# ---------------------------------------------------------------------------
# Provider errors: judgment_error row, then re-raise
# ---------------------------------------------------------------------------

def test_transient_provider_error_records_and_reraises(
    conn, registry_and_snapshot
):
    rig = registry_and_snapshot
    rig["fake_provider"].script_error(TransientProviderError("503"))

    with pytest.raises(TransientProviderError):
        run_judgment(
            conn,
            registry=rig["providers"],
            source_revision_id=rig["revision_id"],
            analysis_run_id=rig["analysis_run_id"],
            prompt_name="capability_summary",
            context=_judgment_context(),
            provider_name="gemini",
            model_id="gemini-2.5-flash",
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT error_class, error_detail FROM judgment_error"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "transient"
    assert "503" in rows[0][1]["message"]


def test_blocked_provider_error_records_policy_class(
    conn, registry_and_snapshot
):
    rig = registry_and_snapshot
    rig["fake_provider"].script_error(BlockedProviderError("no key"))
    with pytest.raises(BlockedProviderError):
        run_judgment(
            conn,
            registry=rig["providers"],
            source_revision_id=rig["revision_id"],
            analysis_run_id=rig["analysis_run_id"],
            prompt_name="capability_summary",
            context=_judgment_context(),
            provider_name="gemini",
            model_id="gemini-2.5-flash",
        )
    with conn.cursor() as cur:
        cur.execute("SELECT error_class FROM judgment_error")
        assert cur.fetchone()[0] == "policy"


def test_classify_provider_error_maps_to_workflow_error_classes():
    assert classify_provider_error(TransientProviderError()) == ErrorClass.TRANSIENT
    assert classify_provider_error(InputProviderError())     == ErrorClass.INPUT
    assert classify_provider_error(BlockedProviderError())   == ErrorClass.POLICY
    assert classify_provider_error(RuntimeError())           == ErrorClass.TRANSIENT


# ---------------------------------------------------------------------------
# Aggregate: provider reliability query
# ---------------------------------------------------------------------------

def test_provider_reliability_is_queryable(conn, registry_and_snapshot):
    """
    Three responses: two valid, one malformed. A simple query over
    judgment_response tells us this provider's reliability rate at
    this prompt version. That query IS the provider-reliability
    evidence surface Step 7 promises.
    """
    rig = registry_and_snapshot
    rig["fake_provider"].script_response(_valid_payload())
    rig["fake_provider"].script_response(_valid_payload())
    rig["fake_provider"].script_response("not json")

    for _ in range(3):
        run_judgment(
            conn,
            registry=rig["providers"],
            source_revision_id=rig["revision_id"],
            analysis_run_id=rig["analysis_run_id"],
            prompt_name="capability_summary",
            context=_judgment_context(),
            provider_name="gemini",
            model_id="gemini-2.5-flash",
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.name,
                   COUNT(*)                              AS total,
                   COUNT(*) FILTER (WHERE r.schema_valid)  AS valid_count
            FROM judgment_response r
            JOIN model_provider p ON p.id = r.provider_id
            GROUP BY p.name
            """
        )
        rows = cur.fetchall()
    assert rows == [("gemini", 3, 2)]
