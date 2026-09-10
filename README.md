# Capability Intelligence Platform

The specs (Build Workflow, PDR, Data Model, State Machine) are the source of truth; this code is one path through them.

Two layers ship in this repo:

- **Steps 0–13** — the Build Workflow foundation: provenance, workflow engine, connectors, analysis, judgment, capability registry, scoring, gates, lifecycle, projects, search-before-build gate.
- **Foundation Phases 1–6** — the composable-component layer built on top: component kinds beyond libraries, repeatable ingestion, LLM-assisted classification/supersession/linking, per-kind scoring, project constraints, pipeline composition + scaffolding, and a Claude skill + CLI (`cip-composer`).

**New to this repo?** Read [`docs/WHAT_IS_CIP.md`](docs/WHAT_IS_CIP.md) for a plain-language overview, or [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the technical reference.

## What's built

### Build Workflow (Steps 0–13)

| Step | Status |
|------|--------|
| 0. Scaffold, env, import-linter contracts | ✅ |
| 1. Migration 001 — provenance chain | ✅ |
| 2. Migration 002 — workflow engine tables | ✅ |
| 3. Workflow engine (transitions, ledger, leases, outbox, retry, dead-letter) | ✅ |
| 4. Connector base (Protocol + registry) + GitHub connector + FakeConnector | ✅ |
| 5. Ingestion state machine — identified → snapshotted | ✅ |
| 6. Static analysis + evidence extractors (append-only) | ✅ |
| 7. Model analysis, single provider (Gemini) | ✅ |
| 8. Capability registry (normalization + supersession) | ✅ |
| 9. Deterministic scoring (profile-as-data, byte-stable hash) | ✅ |
| 10. Policy gates + risk acceptance + publication guard | ✅ |
| 11. Lifecycle (candidate → cataloged, seven guards) | ✅ |
| 12. Projects, requirements, fit evaluation, recommendations | ✅ |
| 13. Search-before-build gate | ✅ |
| 14. LLM-in-chat via MCP server | ✅ (extended by Phases 1–6 below) |

### Foundation Phases (composable-component layer)

| Phase | Status | Ships |
|-------|--------|-------|
| 1. Component model | ✅ | Six kinds (library/repo/agent/skill/mcp_tool/workflow_template), runtime, cost_tier, license_spdx, JSONB I/O types |
| 2. Ingestion beyond libraries | ✅ | Idempotent GitHub / awesome-list / MCP-registry / Claude-skills / Gemini-enrich ingesters, run cursor tracking, cadence doc |
| 3a. LLM classification | ✅ | Component_kind + capability_kind reclassification with confidence + audit |
| 3b. Cross-kind identity | ✅ | `same_component` links between pypi/npm/github ecosystems |
| 3c. Supersession detection | ✅ | Deprecation signals + successor resolution from descriptions |
| 3d. Per-kind scoring | ✅ | Deterministic health scores per component_kind (repo/library/skill/mcp_tool/agent/workflow_template) |
| 4. Project constraints | ✅ | `project_constraint` (cpu_only, must_be_free_tier, license_allowlist, budget_monthly_ceiling, …) with hard-filter integration in browse/search |
| 5a. workflow_template_step + compatibility | ✅ | Chain schema; deterministic compat evaluator with adapter hints |
| 5b. suggest_pipeline | ✅ | Gemini decomposes intent → per-stage picks with edges + gap notes |
| 5c. scaffold_pipeline | ✅ | Turns a proposal into a real repo skeleton |
| 6. Distribution + self-improvement | ✅ | `cip-composer` CLI + Claude skill + meta demo pointing CIP at itself |

## Quickstart (local, with a running Postgres)

You need a running Postgres 14+ and two empty databases: `cip_local` and `cip_test`.
The defaults in `.env.example` assume a local user `cip` with password `cip` on port `5432`.

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env
# edit .env — set DATABASE_URL and TEST_DATABASE_URL to match your Postgres install
pip install -e .            # installs the `cip-mcp` and `cip-composer` scripts
$env:PYTHONPATH = "."
python -m db.migrate up     # applies migrations 001..018 in order
pytest                      # runs all tests (500+)
lint-imports                # enforces the two structural rules
```

**macOS / Linux (bash):**

```bash
cp .env.example .env
pip install -e .
export PYTHONPATH=.
python -m db.migrate up
pytest
lint-imports
```

Notes:
- Windows Postgres installer sometimes claims port `5433` if `5432` is taken. Check with `netstat -ano | findstr LISTEN` and update the port in `.env`.
- `GITHUB_TOKEN` is optional — public-repo reads work without it, but a token raises the rate limit substantially. Set via user env vars, not in `.env`, if you want to avoid leak risk during a Claude session.
- `GEMINI_API_KEY` is needed for Step 7 (judgment), Phase 2e (enrichment), Phase 3a/3b/3c (classification/linking/supersession), and Phase 5b (pipeline suggestion). Same secret-hygiene rule.

## Using it

Once installed and the migrations applied:

```powershell
cip-composer info                                     # registry stats
cip-composer find "video editing" --kind repo --limit 10
cip-composer suggest "transcribe audio then subtitle it" --out proposal.json
cip-composer scaffold --proposal proposal.json --out-dir ./my-pipeline
cip-composer flow "intent here" --out-dir ./my-pipeline    # all in one
```

From inside a Claude Code session with the CIP MCP server registered (`claude mcp add cip -- cip-mcp`), the same surface is available as MCP tools:
`mcp__cip__search_capabilities`, `browse_components`, `capability_detail`, `capability_constraint_fit`, `capability_compatibility`, `suggest_pipeline`, `scaffold_pipeline`.

A companion Claude skill lives at `~/.claude/skills/cip-composer/SKILL.md` — it teaches Claude when to invoke CIP and how to hold the conversation.

## Adding CIP to another project

CIP ships in three shapes so you can add it to any build:

- **Python package** — `pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git` (drops `cip-mcp`, `cip-composer`, and `cip-bootstrap` on PATH)
- **MCP server** — `claude mcp add cip -- cip-mcp` after installing
- **Claude plugin** — bundles the skill and the MCP server together via `claude plugin install cip-composer` (marketplace lives at [`plugins/`](plugins/))

Full walkthrough: [`docs/INSTALL_IN_OTHER_PROJECTS.md`](docs/INSTALL_IN_OTHER_PROJECTS.md).
Prerequisite for all three: a Postgres 14+ instance with the CIP schema applied.

## Ingestion cadence

See [`docs/ingestion_cadence.md`](docs/ingestion_cadence.md) for scheduled-task recipes.

## Layout

```
.
├── api/                FastAPI app — stubs only (superseded by mcp_server + cip_composer)
├── mcp_server/         MCP server exposing the registry to LLMs
├── cip_composer/       cip-composer CLI package
├── core/               Domain logic — no HTTP, no framework imports
│   ├── workflow/       Workflow engine (Step 3)
│   ├── capability/     Registry, normalization, supersession (Step 8)
│   ├── scoring/        Deterministic scoring, profile-as-data (Step 9)
│   ├── policy/         Gates, risk acceptance, publication guard, constraints, compatibility (Steps 10 + Phases 4/5a)
│   ├── judgment/       Model-analysis contracts (Step 7)
│   └── project/        Requirements, fit, recommendations (Step 12)
├── connectors/         Source ecosystems — github, mcp, fake (Step 4+)
├── analysis/           Static analysis + evidence extractors (Step 6+)
├── workers/            Queue consumers (Step 5+)
├── scripts/
│   ├── ingest/         Repeatable ingesters (Phase 2)
│   ├── judge/          LLM classifiers, linkers, deprecation detectors (Phase 3a/3b/3c)
│   ├── score/          Deterministic per-kind scoring worker (Phase 3d)
│   └── compose/        suggest_pipeline + scaffold_pipeline (Phase 5b/5c)
├── db/                 Migrations 001..018 and connection helpers
├── config/             providers.yaml, scoring_profiles/, recommendation_rules/
├── tests/              pytest suite
└── docs/               ingestion_cadence.md and future architecture notes
```

## Rules that outrank the layout

- `core/` must not import from `api/` or `workers/`. Enforced by import-linter.
- `core/scoring/` must have no import path to any provider client. Same enforcement.
- Schema changes are migrations. Never alter a table in place.
- `state_transition` and `evidence_item` are append-only (enforced at the DB level).
- Every LLM-writable table records `prompt_name + prompt_version + prompt_hash + model_name` for audit. Bumping a prompt version re-runs cleanly.
- Every ingester tracks a run cursor in `ingest_run` and is safe to schedule.
