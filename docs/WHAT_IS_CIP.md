# What is CIP? (plain-language overview)

## The problem it solves

Every time you start building something — an app, a feature, a pipeline — you face the same question: **has someone already built part of this, and can I use it instead of writing it myself?**

Normally you answer that by searching GitHub, asking around, or just guessing and building from scratch. That search is slow, scattered across different places (package registries, GitHub, AI-tool catalogs), and easy to skip when you're in a hurry — which is exactly when you end up reinventing something that already existed.

CIP (Capability Intelligence Platform) is a **searchable catalog of software building blocks**, plus a helper that reads what you're trying to build and tells you what already exists for it — honestly, including when nothing does.

## What's actually in the catalog

Not just code libraries. CIP treats five different *kinds* of things as equally searchable:

- **Libraries** — packages you'd `pip install` or `npm install` (e.g. `fastapi`, `react`)
- **Repos** — whole GitHub projects you could fork, run, or learn from (e.g. a video editor, a curated tool list)
- **Skills** — instruction files that teach an AI assistant how to do a specific task
- **MCP tools** — callable services an AI assistant can use directly
- **Agents** — pre-built AI helpers scoped to a particular job

As of today, the catalog holds **448 real components** pulled from GitHub, curated lists, and locally-installed AI tools — not a demo set, actual ingested data.

## How you'd actually use it

**Scenario 1 — "I want to build X."**
You describe what you're building in plain English. CIP breaks it into stages (e.g. "transcribe audio → cut scenes → add subtitles → export video"), searches the catalog for each stage, and hands back a shortlist per stage — ranked, with a note on whether two picks would actually work together or need glue code between them.

**Scenario 2 — "What's already out there for X?"**
You ask a narrower question — "what repos exist for video editing" — and get back real, ranked results with a one-line description of what each one does.

**Scenario 3 — "Build me the skeleton."**
Once you're happy with the picks, CIP writes an actual folder structure — one subfolder per stage, each with notes on what to wire up, an env-var checklist, and the alternatives you didn't pick. You (or a developer) fill in the actual code; CIP does the assembly, not the coding.

**Scenario 4 — constraints.**
You can tell CIP "I need this to run on a normal laptop, stay free, and only use permissive-license code" — and every suggestion gets filtered against that before it reaches you.

## Where it lives and how you reach it

You don't need to open a database or write code to use CIP. Three doors in:

1. **Inside a chat with Claude** — once set up, you just talk normally ("I want to build a pipeline that does X") and Claude automatically checks the catalog before suggesting anything.
2. **A command-line tool** (`cip-composer`) — for anyone comfortable typing commands, no chat needed.
3. **A "plugin"** you install once per project — bundles both of the above together.

## What it deliberately does NOT do

- It doesn't write your application code. It writes the *scaffolding* — folders, notes, a checklist — not working software.
- It doesn't run anything for you. Every suggestion is something you review and decide on.
- It doesn't guess when it doesn't know. If nothing in the catalog matches what you asked for, it says so plainly instead of making something up.

## Why this matters in practice

The catalog **grows over time** — every time it's pointed at GitHub or a curated list, it adds real entries and never duplicates what it already has. So the more it's used, the better its answers get, for everyone using the same shared catalog. It also **checks itself**: because CIP itself is listed in its own catalog, you can ask it "what would make you better?" and get a real answer instead of nothing.

## Adding it to a new project — the short version

You (or whoever manages your infrastructure) need one thing set up first: a shared database the catalog lives in. After that's running once, adding CIP to any new project is:

```
claude plugin marketplace add https://github.com/Rasatabula1971/Repo--CIP-Capability-Intelligence-Platform.git
claude plugin install cip-composer
```

Two lines. From then on, that project's AI assistant checks the catalog automatically before suggesting new code.

If you want the full step-by-step (including the one-time database setup), see [`INSTALL_IN_OTHER_PROJECTS.md`](INSTALL_IN_OTHER_PROJECTS.md) — or hand that file to whoever's setting it up; it's written for a developer, not a general reader.
