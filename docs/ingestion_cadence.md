# CIP ingestion cadence

Every ingester in `scripts/ingest/` is idempotent and safe to schedule.
This doc says *what to run when*. Adjust to match how much the source
actually changes.

## Ingesters

| Ingester | Source | Cost per full run | Suggested cadence |
|----------|--------|-------------------|-------------------|
| `github_search` | GitHub search API | ~1 req / repo | Nightly if you're active; weekly otherwise |
| `awesome_list` | One curated `awesome-*` README | ~1 + N reqs, where N = links | Weekly per list, monthly for stable lists |
| `mcp_registry` | Local `~/.claude.json` | Zero network | Whenever you install/remove an MCP server |
| `claude_skills` | Local `~/.claude/plugins/` | Zero network | Whenever you install/update a plugin |
| `gemini_enrich` | Gemini API | 1 Gemini call per unenriched row | On demand after ingestion adds new rows |

## Baseline schedule (single-user, keep-it-cheap)

- **Daily 02:00 local:** `mcp_registry`, `claude_skills`. Both are local, zero cost, catch anything you installed during the day.
- **Weekly Sunday 03:00:** `awesome_list` for each list you care about. Sequential, `--max-repos 200` per list to stay under GitHub's minute-level search cap.
- **Weekly Sunday 04:00:** `github_search --auto-since` for each standing query (video, LLM tooling, whatever you're tracking). `--auto-since` reads the last run's cursor so only new/updated repos hit the API.
- **After any ingestion:** `gemini_enrich --max 50 --kind repo` (repos benefit most from a one-liner). Run again for `--kind skill` and `--kind mcp_tool` if there were new ones.

## Windows Task Scheduler examples

Create tasks with `schtasks` or the GUI. Each task runs one PowerShell command:

**Daily local scans:**

```powershell
schtasks /Create /SC DAILY /ST 02:00 /TN "CIP\daily-local-scans" `
  /TR "powershell -NoProfile -Command \"cd 'C:\CIP\Repo--CIP-Capability-Intelligence-Platform'; python -m scripts.ingest.mcp_registry; python -m scripts.ingest.claude_skills\""
```

**Weekly awesome-list refresh (edit the list):**

```powershell
schtasks /Create /SC WEEKLY /D SUN /ST 03:00 /TN "CIP\weekly-awesome-video" `
  /TR "powershell -NoProfile -Command \"cd 'C:\CIP\Repo--CIP-Capability-Intelligence-Platform'; python -m scripts.ingest.awesome_list --list ad-si/awesome-video-production --max-repos 200\""
```

**Weekly GitHub search (auto-since so only new repos hit the API):**

```powershell
schtasks /Create /SC WEEKLY /D SUN /ST 04:00 /TN "CIP\weekly-video-search" `
  /TR "powershell -NoProfile -Command \"cd 'C:\CIP\Repo--CIP-Capability-Intelligence-Platform'; python -m scripts.ingest.github_search --query 'topic:video-production stars:>50' --auto-since --max-repos 200\""
```

**On-demand enrichment (manual for now — cron once cadence is proven):**

```powershell
cd "C:\CIP\Repo--CIP-Capability-Intelligence-Platform"
python -m scripts.ingest.gemini_enrich --max 50 --kind repo
python -m scripts.ingest.gemini_enrich --max 50 --kind skill
python -m scripts.ingest.gemini_enrich --max 50 --kind mcp_tool
```

## Rate limits worth knowing

- **GitHub authenticated (with `GITHUB_TOKEN`):** 5000 req/hour general, 30 req/min for search. A 200-repo awesome-list run needs ~201 requests — well within budget.
- **GitHub unauthenticated:** 60 req/hour general, 10 req/min for search. Barely enough for cautious runs. Set a token.
- **Gemini 3.5-flash-lite free tier:** ~30 req/min (last measured 2026-09-08). The enricher's `--interval 3` (default 4) stays comfortably below.
- **Gemini 3.6-flash free tier:** ~2-5 req/min — too tight for batch enrichment; the enricher defaults to 3.5-flash-lite for that reason.

## Verifying what a scheduled run did

```powershell
cd "C:\CIP\Repo--CIP-Capability-Intelligence-Platform"
python -c "from db.connection import connect; c = connect(); cur = c.cursor(); cur.execute('SELECT source_name, status, counts, started_at, finished_at FROM ingest_run ORDER BY started_at DESC LIMIT 10'); [print(r) for r in cur.fetchall()]"
```

Shows the last 10 runs with counts and status. Failed runs keep their `error_detail` — look there if a scheduled task went red.

## When *not* to run something

- **Don't re-enrich unless something changed.** `gemini_enrich` skips already-enriched rows automatically, but burning quota on a 400-row registry that hasn't grown is wasted spend.
- **Don't ingest an awesome-list you don't intend to use.** Every ingest fetches per-repo metadata; each row you don't need is a request you wasted.
- **Never run any of these with `.env` open in an external editor mid-run** — a file-change notification landing in a Claude session while ingestion is writing to the same registry can produce confusing race notifications. Close the editor, run, then reopen.

## Related

- Ingest run log lives in the `ingest_run` table. `SELECT * FROM ingest_run_latest` gives the most recent completed run per source.
- Every ingester's source name is the value in `ingest_run.source_name` — grep the scripts for `SOURCE_NAME` if you need to see how they're keyed.
