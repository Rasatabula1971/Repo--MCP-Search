"""
Seed a small demo capability registry so the MCP server has something to
return. Picks packages relevant to the Garage PDR (multi-tenant SaaS,
job/task data model, state machines, photo evidence, voice input, KPIs)
so PDR-decomposition demos have plausible answers.

Idempotent: rerun as often as you want, capability normalized_keys are
unique and rows are ON CONFLICT DO NOTHING / SELECTed back where needed.

Run:
    python -m scripts.seed_demo_registry
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from db.connection import connect


@dataclass
class Interface:
    kind: str      # 'function' | 'class' | 'async_function'
    name: str
    signature: str
    language: str = "python"


@dataclass
class Dependency:
    ecosystem: str
    name: str
    version_spec: str = ""
    dep_kind: str = "runtime"


@dataclass
class DemoCapability:
    normalized_key: str
    display_name: str
    ecosystem: str
    kind: str
    display_version: str
    score: float                     # 0..1 intrinsic score
    confidence: float                # 0..1 scorecard confidence
    interfaces: list[Interface] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)


CATALOG: list[DemoCapability] = [
    DemoCapability(
        normalized_key="pypi:fastapi",
        display_name="fastapi",
        ecosystem="pypi",
        kind="library",
        display_version="0.115.0",
        score=0.92,
        confidence=0.95,
        interfaces=[
            Interface("class", "FastAPI", "FastAPI(**kwargs)"),
            Interface("class", "APIRouter", "APIRouter(prefix='', tags=None)"),
            Interface("class", "Depends", "Depends(dependency)"),
        ],
        dependencies=[
            Dependency("pypi", "starlette", ">=0.37"),
            Dependency("pypi", "pydantic", ">=2.0"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:sqlalchemy",
        display_name="sqlalchemy",
        ecosystem="pypi",
        kind="library",
        display_version="2.0.35",
        score=0.90,
        confidence=0.94,
        interfaces=[
            Interface("class", "Engine", "Engine(url, **kwargs)"),
            Interface("function", "create_engine", "create_engine(url) -> Engine"),
            Interface("class", "Session", "Session(bind=None)"),
        ],
        dependencies=[
            Dependency("pypi", "typing-extensions", ">=4.6"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:pydantic",
        display_name="pydantic",
        ecosystem="pypi",
        kind="library",
        display_version="2.9.0",
        score=0.91,
        confidence=0.96,
        interfaces=[
            Interface("class", "BaseModel", "BaseModel(**data)"),
            Interface("function", "Field", "Field(default=..., **kwargs)"),
        ],
        dependencies=[
            Dependency("pypi", "pydantic-core", "==2.23.0"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:transitions",
        display_name="transitions",
        ecosystem="pypi",
        kind="library",
        display_version="0.9.2",
        score=0.78,
        confidence=0.85,
        interfaces=[
            Interface(
                "class",
                "Machine",
                "Machine(model=None, states=None, transitions=None, initial=None)",
            ),
        ],
        dependencies=[
            Dependency("pypi", "six", ">=1.10"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:celery",
        display_name="celery",
        ecosystem="pypi",
        kind="library",
        display_version="5.4.0",
        score=0.82,
        confidence=0.88,
        interfaces=[
            Interface("class", "Celery", "Celery(main=None, broker=None)"),
            Interface("function", "shared_task", "shared_task(*args, **kwargs)"),
        ],
        dependencies=[
            Dependency("pypi", "kombu", ">=5.3"),
            Dependency("pypi", "billiard", ">=4.2"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:boto3",
        display_name="boto3",
        ecosystem="pypi",
        kind="library",
        display_version="1.35.0",
        score=0.88,
        confidence=0.92,
        interfaces=[
            Interface("function", "client", "client(service_name, **kwargs)"),
            Interface("function", "resource", "resource(service_name, **kwargs)"),
        ],
        dependencies=[
            Dependency("pypi", "botocore", ">=1.35"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:openai-whisper",
        display_name="openai-whisper",
        ecosystem="pypi",
        kind="library",
        display_version="20240930",
        score=0.75,
        confidence=0.82,
        interfaces=[
            Interface("function", "load_model", "load_model(name, device=None)"),
            Interface("function", "transcribe", "transcribe(model, audio) -> dict"),
        ],
        dependencies=[
            Dependency("pypi", "torch", ">=2.0"),
        ],
    ),
    DemoCapability(
        normalized_key="npm:react",
        display_name="react",
        ecosystem="npm",
        kind="library",
        display_version="18.3.1",
        score=0.94,
        confidence=0.96,
        interfaces=[
            Interface("function", "useState", "useState(initial)", "javascript"),
            Interface("function", "useEffect", "useEffect(effect, deps)", "javascript"),
            Interface("function", "createContext", "createContext(default)", "javascript"),
        ],
        dependencies=[],
    ),
    DemoCapability(
        normalized_key="npm:react-hook-form",
        display_name="react-hook-form",
        ecosystem="npm",
        kind="library",
        display_version="7.53.0",
        score=0.85,
        confidence=0.90,
        interfaces=[
            Interface("function", "useForm", "useForm(config)", "javascript"),
            Interface("function", "useController", "useController(props)", "javascript"),
        ],
        dependencies=[
            Dependency("npm", "react", ">=17", "peer"),
        ],
    ),
    DemoCapability(
        normalized_key="npm:xstate",
        display_name="xstate",
        ecosystem="npm",
        kind="library",
        display_version="5.18.0",
        score=0.83,
        confidence=0.89,
        interfaces=[
            Interface("function", "createMachine", "createMachine(config)", "javascript"),
            Interface("function", "createActor", "createActor(logic)", "javascript"),
        ],
        dependencies=[],
    ),
    DemoCapability(
        normalized_key="pypi:sentry-sdk",
        display_name="sentry-sdk",
        ecosystem="pypi",
        kind="library",
        display_version="2.14.0",
        score=0.86,
        confidence=0.91,
        interfaces=[
            Interface("function", "init", "init(dsn, **options)"),
            Interface("function", "capture_exception", "capture_exception(error=None)"),
        ],
        dependencies=[
            Dependency("pypi", "urllib3", ">=1.26"),
        ],
    ),
    DemoCapability(
        normalized_key="pypi:auth0-python",
        display_name="auth0-python",
        ecosystem="pypi",
        kind="library",
        display_version="4.7.2",
        score=0.80,
        confidence=0.87,
        interfaces=[
            Interface("class", "GetToken", "GetToken(domain, client_id, client_secret)"),
            Interface("class", "Users", "Users(domain, token)"),
        ],
        dependencies=[
            Dependency("pypi", "requests", ">=2.28"),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------

def _ensure_provider(cur) -> uuid.UUID:
    cur.execute(
        "INSERT INTO source_provider (name, kind) "
        "VALUES ('demo', 'code_host') "
        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
        "RETURNING id"
    )
    return cur.fetchone()[0]


def _ensure_asset(cur, provider_id: uuid.UUID, external_key: str) -> uuid.UUID:
    cur.execute(
        "INSERT INTO source_asset (provider_id, external_key, display_name, kind) "
        "VALUES (%s, %s, %s, 'repository') "
        "ON CONFLICT (provider_id, external_key) DO UPDATE "
        "  SET display_name = EXCLUDED.display_name "
        "RETURNING id",
        (provider_id, external_key, external_key),
    )
    return cur.fetchone()[0]


def _ensure_revision(cur, asset_id: uuid.UUID, revision_key: str) -> uuid.UUID:
    # source_revision has UNIQUE (source_asset_id, revision_key) — but we can't
    # be sure it does across schema versions, so try-insert then select.
    cur.execute(
        "SELECT id FROM source_revision "
        "WHERE source_asset_id = %s AND revision_key = %s",
        (asset_id, revision_key),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO source_revision (source_asset_id, revision_key) "
        "VALUES (%s, %s) RETURNING id",
        (asset_id, revision_key),
    )
    return cur.fetchone()[0]


def _ensure_analysis_run(cur, rev_id: uuid.UUID) -> uuid.UUID:
    cur.execute(
        "SELECT id FROM analysis_run WHERE source_revision_id = %s LIMIT 1",
        (rev_id,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO analysis_run "
        "(source_revision_id, extractor_config_version, status) "
        "VALUES (%s, 1, 'completed') RETURNING id",
        (rev_id,),
    )
    return cur.fetchone()[0]


def _new_evidence(cur, rev_id: uuid.UUID, run_id: uuid.UUID,
                  evidence_type: str, note: str) -> uuid.UUID:
    cur.execute(
        "INSERT INTO evidence_item "
        "(source_revision_id, analysis_run_id, extractor_name, "
        " evidence_type, locator_kind, locator, extracted_value) "
        "VALUES (%s, %s, 'demo-seed', %s, 'whole_file', %s::jsonb, %s::jsonb) "
        "RETURNING id",
        (
            rev_id, run_id, evidence_type,
            json.dumps({"path": f"demo/{note}"}),
            json.dumps({"note": note}),
        ),
    )
    return cur.fetchone()[0]


def _ensure_scoring_profile(cur) -> uuid.UUID:
    cur.execute(
        "SELECT id FROM scoring_profile WHERE name = 'demo' AND version = 1"
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO scoring_profile "
        "(name, version, profile_hash, dimensions) "
        "VALUES ('demo', 1, 'demo-hash-v1', '{\"dimensions\": []}'::jsonb) "
        "RETURNING id"
    )
    return cur.fetchone()[0]


def seed_one(cur, dc: DemoCapability, provider_id: uuid.UUID,
             profile_id: uuid.UUID) -> str:
    """Insert one demo capability end-to-end. Returns a short status."""
    # 1. capability — idempotent on normalized_key
    cur.execute(
        "INSERT INTO capability "
        "(normalized_key, display_name, ecosystem, kind) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (normalized_key) DO UPDATE "
        "  SET display_name = EXCLUDED.display_name "
        "RETURNING id",
        (dc.normalized_key, dc.display_name, dc.ecosystem, dc.kind),
    )
    cap_id = cur.fetchone()[0]

    # 2. If a head version already exists, skip the rest (idempotent).
    cur.execute(
        "SELECT id FROM capability_version "
        "WHERE capability_id = %s AND superseded_by_id IS NULL LIMIT 1",
        (cap_id,),
    )
    row = cur.fetchone()
    if row:
        return f"skip  {dc.normalized_key} (already seeded)"

    # 3. Build the source ancestry for FKs.
    external_key = f"seed/{dc.normalized_key.replace(':', '_')}"
    asset_id = _ensure_asset(cur, provider_id, external_key)
    rev_key = f"demo-{uuid.uuid4().hex[:8]}"
    rev_id = _ensure_revision(cur, asset_id, rev_key)
    run_id = _ensure_analysis_run(cur, rev_id)

    # 4. Version + binding.
    version_key = f"content:{uuid.uuid4().hex[:12]}"
    cur.execute(
        "INSERT INTO capability_version "
        "(capability_id, version_key, version_kind, display_version) "
        "VALUES (%s, %s, 'content-hash', %s) RETURNING id",
        (cap_id, version_key, dc.display_version),
    )
    ver_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO capability_source_binding "
        "(capability_version_id, source_revision_id) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (ver_id, rev_id),
    )

    # 5. Interfaces (each needs its own evidence_item).
    for iface in dc.interfaces:
        ev_id = _new_evidence(cur, rev_id, run_id, "interface", iface.name)
        cur.execute(
            "INSERT INTO capability_interface "
            "(capability_version_id, evidence_item_id, kind, name, signature, language) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ver_id, ev_id, iface.kind, iface.name, iface.signature, iface.language),
        )

    # 6. Dependencies.
    for dep in dc.dependencies:
        ev_id = _new_evidence(cur, rev_id, run_id, "dependency", dep.name)
        cur.execute(
            "INSERT INTO capability_dependency "
            "(capability_version_id, evidence_item_id, depends_on_ecosystem, "
            " depends_on_name, version_spec, dep_kind) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (ver_id, ev_id, dep.ecosystem, dep.name, dep.version_spec, dep.dep_kind),
        )

    # 7. Scorecard + a single 'health' dimension, so ordering by score works.
    cur.execute(
        "INSERT INTO scorecard "
        "(capability_version_id, scoring_profile_id, total_score, "
        " confidence, computed_hash) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (ver_id, profile_id, dc.score, dc.confidence, uuid.uuid4().hex),
    )
    card_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO score_dimension_result "
        "(scorecard_id, dimension_name, raw_score, weight, "
        " weighted_score, coverage, contradiction, evidence_count) "
        "VALUES (%s, 'health', %s, 1.000, %s, 1.000, 0.000, %s)",
        (card_id, dc.score, dc.score, len(dc.interfaces) + len(dc.dependencies)),
    )

    return f"seed  {dc.normalized_key}  score={dc.score:.2f}"


def main() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            provider_id = _ensure_provider(cur)
            profile_id = _ensure_scoring_profile(cur)
            for dc in CATALOG:
                print(seed_one(cur, dc, provider_id, profile_id))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
