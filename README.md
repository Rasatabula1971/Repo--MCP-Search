# Capability Intelligence Platform

Implementation of Steps 0–3 of the Build Workflow (v1.0). The specs in `docs/` are the source of truth; this code is one path through them.

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
| 14. Read/write API + minimal UI | Not started — see Build Workflow doc |

## Quickstart (local, with a running Postgres)

```
cp .env.example .env
# edit .env — set DATABASE_URL and TEST_DATABASE_URL
export PYTHONPATH=.
python -m db.migrate up      # applies migrations in order
pytest                       # runs all tests
lint-imports                 # enforces the two structural rules
```

## Layout

```
cip/
├── api/               FastAPI app (Step 14)
├── core/              Domain logic — no HTTP, no framework imports
│   └── workflow/      Workflow engine (Step 3)
├── connectors/        Source ecosystems (Step 4+)
├── analysis/          Static analysis extractors (Step 6+)
├── workers/           Queue consumers (Step 5+)
├── db/                Migrations and connection helpers
├── tests/
└── docs/              PDR, Data Model, State Machine, Build Workflow
```

## Rules that outrank the layout

- `core/` must not import from `api/` or `workers/`. Enforced by import-linter.
- `core/scoring/` must have no import path to any provider client. Same enforcement.
- Schema changes are migrations. Never alter a table in place.
- `state_transition`, `evidence_item`, `correction_event` are append-only.
