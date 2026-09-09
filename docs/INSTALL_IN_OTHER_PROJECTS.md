# Adding CIP to another project

CIP is packaged three ways. Pick the shape that fits.

| Shape | Install command | What you get |
|-------|-----------------|--------------|
| Python package | `pip install git+https://github.com/Rasatabula1971/Repo--MCP-Search.git#subdirectory=cip_steps_0_to_13` | `cip-mcp` + `cip-composer` on PATH, importable modules |
| MCP server | `claude mcp add cip -- cip-mcp` | 7 CIP tools in every Claude Code session in that project |
| Claude plugin | `claude plugin marketplace add <path-or-git>/plugins`, then `claude plugin install cip-composer` | Skill + MCP server in one action |

All three share the same runtime — they wrap the same Python package
against the same Postgres registry.

---

## Prerequisites

**Postgres 14+** with two databases: one live (`cip_local`), one for
tests (`cip_test`). CIP owns the schema; the migrations are idempotent.

**Environment variables:**

| Var | Required | Notes |
|-----|----------|-------|
| `DATABASE_URL` | Yes | e.g. `postgresql://cip:cip@localhost:5432/cip_local` |
| `TEST_DATABASE_URL` | For running the test suite | Same shape, different db |
| `GITHUB_TOKEN` | Optional | Raises GitHub API rate limits during ingestion |
| `GEMINI_API_KEY` | For LLM steps | Judgment, enrichment, `suggest_pipeline` |

**Secret hygiene** — set these as **user env vars** on your OS, not in
`.env`, if you're going to be editing anything in a Claude Code session.
See [../README.md](../README.md) for the reason and the PowerShell recipe.

---

## Recommended: shared registry (Path B)

One Postgres, every project points at it, registry data compounds
across every user. This is the shape CIP is designed around — if only
one developer's project ever ingests, the registry stays small and
`browse_components` returns thin results everywhere else. Sharing the
DB removes that gap.

**Bootstrap in one command** (after installing the Python package and
setting your env vars):

```powershell
# Verify what state the DB is in (read-only, no writes).
cip-bootstrap --verify

# First-time set-up: creates the target databases if missing (needs
# CREATEDB privilege OR an --admin-url with one), applies migrations.
cip-bootstrap

# If the shared DB was provisioned for you and you only have USAGE:
cip-bootstrap --skip-create

# Force use of a specific admin channel for CREATE DATABASE:
cip-bootstrap --admin-url "postgresql://postgres@shared-host:5432/postgres"
```

`cip-bootstrap` is idempotent — safe to re-run against an already-set-up
DB (it'll skip existing databases and applied migrations, then print a
summary of the current state).

Wiring the MCP server per project is unchanged from Path A.

## Path A — one-off local install (individual developer)

```powershell
# 1. Install the package. Adds cip-mcp + cip-composer to PATH.
pip install git+https://github.com/Rasatabula1971/Repo--MCP-Search.git#subdirectory=cip_steps_0_to_13

# 2. Create the databases (Postgres already running).
createdb cip_local
createdb cip_test

# 3. Point at them.
[Environment]::SetEnvironmentVariable('DATABASE_URL','postgresql://cip:cip@localhost:5432/cip_local','User')
[Environment]::SetEnvironmentVariable('TEST_DATABASE_URL','postgresql://cip:cip@localhost:5432/cip_test','User')

# 4. Apply migrations (18 files, idempotent). Either raw:
python -m db.migrate up
# Or via the bootstrap script (also creates missing DBs):
cip-bootstrap --skip-create

# 5. Register the MCP server in the current project.
claude mcp add cip -- cip-mcp

# 6. Confirm.
cip-composer info
```

Restart your Claude Code session; `/mcp` should show `cip` connected
with 7 tools.

---

## Path B — shared registry (full setup, manual)

Covered by the **Recommended** section at the top of this file using
`cip-bootstrap`. This section is what the bootstrap does under the hood
if you want to run each step by hand or bake it into your own tooling.

```powershell
pip install git+https://github.com/Rasatabula1971/Repo--MCP-Search.git#subdirectory=cip_steps_0_to_13

# Point at the shared instance. User env vars, not .env.
[Environment]::SetEnvironmentVariable('DATABASE_URL','postgresql://cip:<pass>@your-host:5432/cip','User')
[Environment]::SetEnvironmentVariable('TEST_DATABASE_URL','postgresql://cip:<pass>@your-host:5432/cip_test','User')

# One-shot: cip-bootstrap does all the below in one command.
cip-bootstrap
# Or, step by step (equivalent):
python -m db.migrate up                                # migrations, idempotent
python -m db.migrate up --test                         # migrations for the test db too

# Register the MCP server per project as usual.
claude mcp add cip -- cip-mcp
```

Every developer sees the same registry after this. Ingest once, use
everywhere. The judgment tables (`component_classification`,
`capability_link`) also stay shared, so a classification one person
runs benefits everyone.

---

## Path C — Claude plugin (skill + MCP in one action)

If you want to bundle the skill file (voice rules, when-to-invoke
guidance) with the MCP server registration:

```bash
# From your project root, add the marketplace this plugin lives in.
# Either a local path or a git URL:
claude plugin marketplace add path/to/Repo--MCP-Search/cip_steps_0_to_13/plugins

# Install the plugin.
claude plugin install cip-composer
```

The plugin drops the skill into your project's skill list and registers
the `cip` MCP server automatically. You still need the Python package
installed (Path A step 1) so `cip-mcp` exists on PATH.

---

## Ingesting real components

Empty registry returns empty search results. Grow it with the ingesters:

```powershell
# Idempotent — re-runs skip unchanged rows.
python -m scripts.ingest.github_search --query "topic:mcp stars:>50" --max-repos 100
python -m scripts.ingest.awesome_list --list ad-si/awesome-video-production
python -m scripts.ingest.mcp_registry               # scans your local ~/.claude.json
python -m scripts.ingest.claude_skills              # scans your local ~/.claude/plugins/

# Deterministic per-kind scoring.
python -m scripts.score.component_scores

# Optional: Gemini one-liners for new rows (needs GEMINI_API_KEY).
python -m scripts.ingest.gemini_enrich --max 50 --kind repo
```

See [`ingestion_cadence.md`](ingestion_cadence.md) for scheduled-task
recipes to keep it fresh.

---

## Using it from another project

Once installed and running:

```powershell
cip-composer info                                  # registry stats
cip-composer find "video editing" --kind repo
cip-composer suggest "an intent" --out proposal.json
cip-composer scaffold --proposal proposal.json --out-dir ./new-pipeline
cip-composer flow "an intent" --out-dir ./new-pipeline    # all in one
```

Or, from inside Claude Code with the MCP server registered:

```
mcp__cip__search_capabilities(query="video editing")
mcp__cip__suggest_pipeline(intent="pipeline description", project_id=<uuid>)
mcp__cip__scaffold_pipeline(proposal=<dict>, out_dir="./new-pipeline")
```

The skill (Path C) teaches Claude *when* to invoke these; without it
you'd need to prompt Claude explicitly.

---

## Uninstalling

```powershell
claude mcp remove cip
claude plugin uninstall cip-composer
pip uninstall cip
# Optionally drop the databases:
dropdb cip_local
dropdb cip_test
```

The `~/.claude/skills/cip-composer/` skill file (if you dropped it manually
via Path A/B rather than the plugin) is a plain file you can `rm` at will.
