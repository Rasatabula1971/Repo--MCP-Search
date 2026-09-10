# cip-composer (Claude plugin)

Add CIP's composable-component registry to any Claude Code project as a
one-shot install: Claude gets the skill *and* the MCP server registration
in one step, no manual `claude mcp add`.

## What the plugin ships

- **`skills/cip-composer/SKILL.md`** — teaches Claude when to invoke CIP
  and how to hold the conversation (direct/dry voice, pressure-test the
  premise, honest gap-flagging).
- **`.claude-plugin/plugin.json`** — declares the `cip` MCP server as a
  stdio process running `cip-mcp`, so `/plugin install` wires both the
  skill and the tools in one action.

## Prerequisites

The plugin's MCP server needs the CIP Python package installed and a
Postgres registry it can talk to. Two paths — pick one:

### Path A — local Postgres per developer

Each developer runs their own Postgres and their own CIP registry.
Fastest to set up, but browse/search return only what THIS developer
has ingested.

```bash
# Install the package (creates cip-mcp and cip-composer commands)
pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git

# Create databases + apply migrations
createdb cip_local
createdb cip_test
export DATABASE_URL="postgresql://cip:cip@localhost:5432/cip_local"
export TEST_DATABASE_URL="postgresql://cip:cip@localhost:5432/cip_test"
python -m db.migrate up

# Optionally seed with the demo catalog
python -m scripts.seed_demo_registry
```

### Path B — shared Postgres (recommended for teams)

One Postgres instance somewhere, every project points at it via
`DATABASE_URL`. The registry accumulates across projects and stays
current for everyone.

```bash
pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git

# Point at the shared instance (URL kept in user env vars, never in
# a repo-tracked .env)
[Environment]::SetEnvironmentVariable('DATABASE_URL','postgresql://cip:...@shared-host:5432/cip','User')

# Migrations still safe to re-apply (they're idempotent by filename)
python -m db.migrate up
```

## Installing the plugin

Once prerequisites are met, from the project root:

```bash
# Add the marketplace this plugin lives in (path or git URL)
claude plugin marketplace add path/to/Repo--CIP-Capability-Intelligence-Platform/plugins

# Install the plugin
claude plugin install cip-composer
```

Restart the session and:
- `/mcp` shows `cip` connected with 7 tools.
- Skills listing shows `cip-composer` available.
- Typing "I want to build X" now triggers Claude to check the registry
  before proposing new code.

## Verifying

```bash
cip-composer info                 # prints registry counts + top-scored components
cip-composer suggest "test intent" --max-stages 3
```

The MCP tools should also be callable inside Claude Code as
`mcp__cip__search_capabilities`, `mcp__cip__suggest_pipeline`, etc.

## Registering the MCP server without the plugin

If you'd rather not use the plugin format and just want the MCP server:

```bash
pip install git+https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git
claude mcp add cip -- cip-mcp
```

Then drop the skill file manually into `~/.claude/skills/cip-composer/SKILL.md`.
