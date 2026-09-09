# CIP — technical architecture

Audience: engineers integrating, extending, or operating this system.
For a plain-language explanation, see [`WHAT_IS_CIP.md`](WHAT_IS_CIP.md).
For install steps, see [`INSTALL_IN_OTHER_PROJECTS.md`](INSTALL_IN_OTHER_PROJECTS.md).

## Repo layout

```
cip_steps_0_to_13/
├── core/                Domain logic — no HTTP, no framework imports
│   ├── workflow/        Workflow engine (transitions, ledger, outbox)
│   ├── capability/      Registry normalization + supersession
│   ├── scoring/         Deterministic evidence-based scoring (Step 9)
│   ├── policy/          Gates, constraints, compatibility (Step 10 + Phase 4/5a)
│   ├── judgment/        Model-provider abstraction (Step 7)
│   └── project/         Requirements, fit evaluation, recommendations
├── connectors/          Source ecosystems (GitHub, MCP, fake/test)
├── analysis/            Static analysis + evidence extractors
├── workers/             Queue consumers for the Step 0-13 workflow engine
├── mcp_server/          MCP server exposing the registry to LLMs
├── cip_composer/        CLI package (`cip-composer` console script)
├── scripts/
│   ├── ingest/          Repeatable ingesters (Phase 2)
│   ├── judge/           LLM classifiers/linkers/deprecation detectors (Phase 3)
│   ├── score/           Deterministic per-kind scoring worker (Phase 3d)
│   ├── compose/         suggest_pipeline + scaffold_pipeline (Phase 5)
│   └── bootstrap.py     One-command DB setup (create + migrate)
├── plugins/cip-composer/ Claude plugin (skill + MCP server bundle)
├── db/migrations/       18 SQL migrations, applied in order, checksum-tracked
├── tests/               512 tests, pytest
└── docs/                This file, cadence doc, install doc, WHAT_IS_CIP
```

## Data model

### Core registry (Step 8, extended in Phase 1)

`capability` is the central table. Every discoverable thing — a pypi
package, a GitHub repo, a Claude skill, an MCP server — is one row.

| Column | Purpose |
|---|---|
| `normalized_key` | Stable identity, e.g. `pypi:requests`, `source:github:psf/requests`, `skill:frontend-design/frontend-design`, `mcp:cip` |
| `component_kind` | Format: `library` \| `repo` \| `agent` \| `skill` \| `mcp_tool` \| `workflow_template` |
| `kind` (capability_kind) | Semantic role: `library` \| `cli` \| `service` \| `framework` \| `tool` \| `dataset` \| `doc` \| `template` |
| `runtime` | How to invoke it: `python_import`, `npm_import`, `mcp_stdio`, `mcp_sse`, `mcp_http`, `claude_skill`, `claude_agent`, `cli_subprocess`, `git_clone`, `http_endpoint`, `other` |
| `cost_tier` | `free` \| `free_tier` \| `cheap_paid` \| `paid` |
| `license_spdx` | SPDX identifier, nullable |
| `metadata` | JSONB — stars, description, `gemini_description`, topics, language, `html_url`, etc. |

`capability_version` → `capability_interface` (with nullable
`input_type`/`output_type` JSONB for typed I/O) → `capability_dependency`
carry the Step 6-9 evidence chain: every claim resolves to a byte range
in a pinned source revision via `evidence_item`.

### Provenance chain (Step 1)

`source_provider` → `source_asset` → `source_revision` → `evidence_item`.
Append-only. Every ingested fact traces back to a specific commit.

### Judgment ledger (Phase 3)

| Table | Written by | Purpose |
|---|---|---|
| `component_classification` | `scripts/judge/classify_components.py` | LLM verdict on component_kind/capability_kind, with confidence + prior-state snapshot |
| `capability_link` | `scripts/judge/link_identities.py`, `scripts/judge/detect_supersession.py` | Typed edges: `same_component`, `superseded_by`, `alternative_to`, `part_of`, `depends_on_symbolic` |
| `component_score` | `scripts/score/component_scores.py` | Deterministic per-kind health score, 0-1, with dimension breakdown |

Every LLM-writable row records `prompt_name + prompt_version + prompt_hash + model_name`.
Bumping `PROMPT_VERSION` in a worker re-classifies everything on next run;
old rows stay for audit.

### Ingestion tracking (Phase 2)

`ingest_run` (+ `ingest_run_latest` view) — one row per ingester
execution: `source_name`, `cursor`, `counts` (new/updated/unchanged/
deprecated/errors), `status`. Every ingester in `scripts/ingest/`
reads its own last-completed cursor via `_base.last_cursor()` and is
safe to re-run or cron.

### Constraints + composition (Phase 4-5)

- `project` / `project_constraint` — per-project hard filters (`cpu_only`,
  `must_be_free_tier`, `must_be_local`, `always_off_at_night`,
  `budget_monthly_ceiling`, `license_allowlist`, `license_denylist`,
  `runtime_allowlist`, `component_kind_allowlist`). Evaluated by the
  pure function `core/policy/constraints.py::evaluate()`.
- `workflow_template_step` — ordered stages of a saved pipeline template,
  each pointing at a `capability` with a `role` (`producer` \|
  `transformer` \| `adapter` \| `sink` \| `gate`).
- `core/policy/compatibility.py::check_pair()` — pure runtime-compatibility
  matrix (`NATIVE_INTEROP`, `BRIDGEABLE`) plus I/O-type comparison,
  returning `compatible` \| `adapter_needed` \| `incompatible` with an
  adapter hint string.

## Ingesters (`scripts/ingest/`)

All share `_base.py`: `run()` context manager (opens/closes an
`ingest_run` row, rolls back on exception), `ensure_provider/asset/
revision`, `upsert_component` (idempotent insert-or-update with real
change detection — compares *effective* values, not raw equality, so
COALESCE-preserved fields don't false-flag as changed).

| Ingester | Source | Writes |
|---|---|---|
| `github_search.py` | GitHub Search API | `component_kind='repo'` rows with stars/license/topics/pushed_at |
| `awesome_list.py` | One `awesome-*` README | Extracts `github.com/owner/repo` links, feeds each through `github_search.ingest_one` |
| `mcp_registry.py` | Local `~/.claude.json` | `component_kind='mcp_tool'` rows (env var *names* only, never values) |
| `claude_skills.py` | `~/.claude/skills/` + plugin marketplaces | `component_kind='skill'` rows, content-hash change detection |
| `gemini_enrich.py` | Gemini API | Backfills `metadata.gemini_description` for rows missing one, cached by prompt hash |

Cadence and rate-limit notes: [`ingestion_cadence.md`](ingestion_cadence.md).

## Judgment workers (`scripts/judge/`)

All Gemini-first (`gemini-3.5-flash-lite`, chosen after `2.5-flash`
deprecated and `3.6-flash`'s free tier proved too rate-limited).
Structured JSON responses (`responseMimeType=application/json`),
strict schema validation, markdown-fence tolerance.

- `classify_components.py` — proposes `component_kind`/`capability_kind`;
  applies the change only if confidence ≥ 0.80 AND it actually differs
  from the current row.
- `link_identities.py` — shared-tail-name heuristic finds
  library↔repo candidate pairs (e.g. `pypi:requests` ↔
  `source:github:psf/requests`), asks the model, writes `same_component`
  links at confidence ≥ 0.75. Negative answers are stored too (as a
  suppressed row) so re-runs don't re-ask.
- `detect_supersession.py` — flags deprecated/retired components from
  description language; resolves a named successor to an existing
  registry row when possible and links `superseded_by`.

## Scoring (`scripts/score/component_scores.py`)

Deterministic, no LLM. One scoring function per `component_kind`,
weights sum to 1.0:

```
repo_health      = 0.30 freshness + 0.30 popularity + 0.10 activity
                  + 0.15 license_clarity + 0.15 description_quality
library_health   = 0.35 license + 0.40 description + 0.25 ecosystem_bonus
skill_health     = 0.50 completeness + 0.40 description + 0.10 license
mcp_tool_health  = 0.60 transport_clarity + 0.40 description
agent_health     = 1.00 description
workflow_template_health = 1.00 completeness
```

`confidence` = fraction of dimensions with real evidence (unscored
inputs default to 0.5 and don't count toward confidence).

## Composition (`scripts/compose/`)

**`suggest_pipeline.py`** — the core LLM-assisted composition tool:

1. Gemini decomposes a plain-English intent into 3-12 ordered stages
   (`name`, `purpose`, `search_terms[]`, `role`, `preferred_component_kind`).
2. Per stage: unions `search_capabilities()` results across every search
   term (optionally hard-filtered by a `project_id`'s constraints),
   dedupes, ranks by `component_score.total_score`, keeps top 3.
3. Adjacent-stage picks run through `core.policy.compatibility.check_pair`
   → annotated edges with verdict + adapter hint.
4. Stages with zero candidates get a gap note — **never fabricates a
   pick**. This is deliberate: a `[]` result is signal, not failure.

Accepts an optional `conn` param so tests inject the fixture connection
without touching the production registry.

**`scaffold_pipeline.py`** — pure I/O, no LLM, no DB. Turns a
`suggest_pipeline` proposal into a real directory:

```
<out_dir>/
  pipeline.yaml        structured chain definition
  README.md            overview + stage list + notes
  .env.example         env vars auto-detected from picks
                        (token-matched: openai/anthropic/gemini/sentry/
                         auth0/aws/github/elevenlabs/runway/tiktok/...)
  stages/NN_<slug>/
    README.md           pick + alternatives + incoming-edge verdict
    TODO.md             explicit wiring checklist
```

Deliberately skeletal — no runnable code is generated. The human wires
the chain; CIP assembles the parts and documents the seams.

## MCP surface (`mcp_server/`)

FastMCP-based stdio server (`cip-mcp` console script). Seven tools:

| Tool | Backing query |
|---|---|
| `search_capabilities` | Keyword + filters, optional `project_id` constraint filtering |
| `browse_components` | No-keyword enumeration, same filters |
| `capability_detail` | Full record: metadata, head version, interfaces, dependencies, scorecard |
| `capability_constraint_fit` | Per-constraint verdict for one component against one project |
| `capability_compatibility` | Runtime + I/O-type compat verdict for a pair |
| `suggest_pipeline` | Wraps `scripts/compose/suggest_pipeline.py` |
| `scaffold_pipeline` | Wraps `scripts/compose/scaffold_pipeline.py` |

`mcp_server/queries.py` holds every pure DB-query function (no MCP
types), independently testable.

## CLI (`cip_composer/cli.py`)

Console script `cip-composer`. Subcommands: `find`, `browse`,
`suggest`, `scaffold`, `flow` (suggest+scaffold in one call), `info`.
Table or `--json` output. Same `.env`-driven config as everything else.

## Bootstrap (`scripts/bootstrap.py`)

`cip-bootstrap` console script. Idempotent one-command setup against a
fresh or existing Postgres: connectivity check → `CREATE DATABASE` (skipped
if it exists, or if `--skip-create`) → `db.migrate.up()` for live +
test DBs → optional `--seed` → prints a state summary. `--verify` runs
read-only. Exit codes: `1` missing env, `2` unreachable Postgres, `3`
insufficient privilege on CREATE DATABASE. Every URL in output is
credential-redacted.

## Distribution (`plugins/cip-composer/`)

A Claude Code plugin: `.claude-plugin/plugin.json` declares the `cip`
MCP server (`stdio`, command `cip-mcp`); `skills/cip-composer/SKILL.md`
is the same skill installed at `~/.claude/skills/cip-composer/SKILL.md`.
`plugins/.claude-plugin/marketplace.json` makes the `plugins/` directory
installable via `claude plugin marketplace add <path-or-git-url>`.

## Structural rules enforced by `import-linter`

- `core/` must not import from `api/` or `workers/`.
- `core/scoring/` has no import path to any provider client
  (`core.judgment`, `connectors`, `google`, `openai`, `anthropic` all
  forbidden as transitive imports).

## Append-only / audit invariants

- `state_transition`, `evidence_item` — append-only at the DB level.
- Every migration file is checksum-tracked in `schema_migration`;
  editing an applied migration raises at runtime. New changes are
  always a new numbered file.
- Every LLM decision (classification, linking, supersession) is
  recorded even when it doesn't change anything — negative answers and
  low-confidence answers are retained specifically so re-runs don't
  re-ask the same question.

## Current live state (as of this doc)

- 18 migrations applied.
- 512 tests passing.
- 448 real components in the registry: 400 repos, 30 skills, 14
  libraries, 2 MCP tools, 1 agent, 1 workflow template.
- Ingested from: targeted GitHub searches, `ad-si/awesome-video-production`,
  `krzemienski/awesome-video`, `punkpeye/awesome-mcp-servers`,
  `steven2358/awesome-generative-ai`, `Hannibal046/Awesome-LLM`, the
  local `~/.claude.json` MCP registrations, and local Claude skill/plugin
  scan.

## Extending CIP

- **New component kind**: extend the CHECK constraint in migration
  `011_component_kinds.sql`'s successor (new migration), add a case to
  `core/policy/compatibility.py`'s `NATIVE_INTEROP`, and a scorer
  branch in `scripts/score/component_scores.py::SCORERS`.
- **New ingester**: implement against `scripts/ingest/_base.py`'s
  helpers; must be idempotent and cursor-tracked (see
  `feedback-cip-ingestion-and-llm-rules` in project memory for the
  hard requirement).
- **New constraint kind**: add to the CHECK constraint in
  `017_project_constraints.sql`'s successor and add a checker function
  in `core/policy/constraints.py::_CHECKS`.
- **New judgment worker**: follow the `scripts/judge/*.py` pattern —
  Gemini call with `responseMimeType=application/json`, strict parse
  function that raises `MalformedResponse` on any schema deviation,
  record-then-maybe-apply split so audit survives even suppressed
  decisions.
