# CIP MCP Server

Exposes the CIP capability registry as MCP tools an LLM can call. The point
of Step 14 (path B): the LLM lives in the user's chat client (Claude,
ChatGPT), the registry lives here, and this server is the bridge.

## Tools

| Tool | What it returns |
|------|-----------------|
| `search_capabilities(query, ecosystem?, kind?, limit=20)` | Ranked list — display name, normalized key, ecosystem, kind, head version, and intrinsic score/confidence when a scorecard exists. |
| `capability_detail(capability_id)` | Full record for one capability: metadata, head version, interfaces, declared dependencies, and the head version's scorecard. Every interface and dependency carries an `evidence_item_id`. |

More tools land in later slices — `evaluate_fit`, `authorize_build`, etc.

## Prerequisites

- Postgres running with the CIP migrations applied (see the top-level [README](../README.md#quickstart-local-with-a-running-postgres)).
- `.env` populated so `DATABASE_URL` resolves.
- The `cip` package installed in the environment you'll run the server from:

  ```powershell
  pip install -e .
  ```

## Register with Claude Code

```powershell
claude mcp add cip -- cip-mcp
```

If the `cip-mcp` script is not on PATH, use the module form:

```powershell
claude mcp add cip -- python -m mcp_server.server
```

After registering, `/mcp` inside Claude Code should show `cip` connected
with the tools listed above.

## Register with Claude Desktop

Add an entry to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cip": {
      "command": "cip-mcp",
      "cwd": "C:\\CIP\\Repo--CIP-Capability-Intelligence-Platform"
    }
  }
}
```

`cwd` matters — the server reads `.env` from the working directory.

## Smoke test without Claude

The tool functions are testable directly against a running Postgres:

```powershell
python -c "from db.connection import connect; from mcp_server.queries import search_capabilities; c = connect(); print(search_capabilities(c, 'http'))"
```
