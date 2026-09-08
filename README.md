# Capability Intelligence Platform

Implementation of Steps 0–13 of the Build Workflow (v1.0). The Build Workflow, PDR, Data Model, and State Machine specs are the source of truth; this code is one path through them.

## What's built

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
| 14. LLM-in-chat via MCP server (search + detail tools) | 🟡 in progress — see [mcp_server/README](mcp_server/README.md) |

## Quickstart (local, with a running Postgres)

You need a running Postgres 14+ and two empty databases: `cip_local` and `cip_test`.
The defaults in `.env.example` assume a local user `cip` with password `cip` on port `5432`.

**Windows (PowerShell):**

```powershell
Copy-Item .env.example .env
# edit .env — set DATABASE_URL and TEST_DATABASE_URL to match your Postgres install
$env:PYTHONPATH = "."
python -m db.migrate up      # applies migrations in order
pytest                       # runs all tests
lint-imports                 # enforces the two structural rules
```

**macOS / Linux (bash):**

```bash
cp .env.example .env
# edit .env — set DATABASE_URL and TEST_DATABASE_URL
export PYTHONPATH=.
python -m db.migrate up
pytest
lint-imports
```

Notes:
- The Windows Postgres installer sometimes claims port `5433` if `5432` is already in use. Check with `netstat -ano | findstr LISTEN` and update the port in `.env` if needed.
- `GITHUB_TOKEN` is optional — public-repo reads work without it, but a token raises the rate limit substantially.
- `GEMINI_API_KEY` is only needed for Step 7 (model analysis) onward.

## Layout

```
.
├── api/               FastAPI app — stubs only (superseded by mcp_server for path B)
├── mcp_server/        MCP server exposing the registry to LLMs (Step 14)
├── core/              Domain logic — no HTTP, no framework imports
│   ├── workflow/      Workflow engine (Step 3)
│   ├── capability/    Registry, normalization, supersession (Step 8)
│   ├── scoring/       Deterministic scoring, profile-as-data (Step 9)
│   ├── policy/        Gates, risk acceptance, publication guard (Step 10)
│   ├── judgment/      Model-analysis contracts (Step 7)
│   └── project/       Requirements, fit, recommendations (Step 12)
├── connectors/        Source ecosystems — github, mcp, fake (Step 4+)
├── analysis/          Static analysis + evidence extractors (Step 6+)
├── workers/           Queue consumers (Step 5+)
├── db/                Migrations and connection helpers
├── config/            providers.yaml, scoring_profiles/, recommendation_rules/
├── scripts/           (empty)
├── tests/             pytest suite — one test_stepN_*.py per step
└── docs/              (empty — source specs live outside this repo)
```

## Rules that outrank the layout

- `core/` must not import from `api/` or `workers/`. Enforced by import-linter.
- `core/scoring/` must have no import path to any provider client. Same enforcement.
- Schema changes are migrations. Never alter a table in place.
- `state_transition` and `evidence_item` are append-only (enforced at the DB level).
