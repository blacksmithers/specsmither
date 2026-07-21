<p align="center">
  <img src="assets/specsmither-logo.svg" alt="SpecSmither" width="420" />
</p>

<p align="center">
  Open-source, local-first, <b>NO-LLM</b> Control System for Agentic AI Implementation
</p>

<p align="center">
  <a href="https://pypi.org/project/specsmither/"><img src="https://img.shields.io/pypi/v/specsmither.svg" alt="PyPI"></a>
  <a href="https://github.com/blacksmithers/specsmither/actions/workflows/ci.yml"><img src="https://github.com/blacksmithers/specsmither/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/pypi/pyversions/specsmither.svg" alt="Python versions">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0">
</p>

---

SpecSmither drives a software scope through `Specification → Epic → Ticket → DAG`
and two lifecycles — **planning** (authoring a spec under a scoring gate) and
**work** (building each ticket under a completion gate) — entirely over a single
SQLite file. The engine is fully deterministic and synchronous; all AI reasoning
lives in the MCP *client*, never in the engine.

It imports two independent, pure gates side-by-side:

- **[crucible](https://github.com/blacksmithers/crucible)** (`crucible-forge`) — the
  planning gate (rubric scorer). A published dependency from day one.
- **assay** (`assayforge → assay`) — the work-session completion gate (0.2.0, upcoming).

## Status

**0.1.0 — the planning release.** The deterministic substrate + the full planning
lifecycle (authoring a spec `draft → ready` through the real crucible gate) + the
agent surface (MCP server, TOON) + the human surface (the Textual TUI Observatory).
The **work** lifecycle (building tickets under the `assay` gate) lands in 0.2.0.

## Install

```sh
uv tool install specsmither      # or: pipx install specsmither  /  pip install specsmither
```

This installs two commands:

| Command | Surface | Notes |
|---|---|---|
| `specsmither` | the **TUI Observatory** — the only human UI | needs a real terminal |
| `specsmither-mcp` | the **MCP stdio server** for the agent | 21 tools, TOON wire |

## Quickstart

```sh
cd my-project
specsmither          # opens the Observatory; press `i` to init the workspace
```

`init` lazily creates the user-global DB, binds this workspace to a project
(`.specsmither/config.json`), and scaffolds `.claude/` (agent skills, subagents,
opt-in read-only hooks + a statusline) so an agent can drive the *same* DB via
`specsmither-mcp` in parallel. Then bootstrap a spec and drive planning:

| Key | Action | Key | Action |
|---|---|---|---|
| `1`–`5` | switch tabs (Browser / DAG / Planning / Work / Search) | `i` | init workspace |
| `c` / `e` / `d` | create / edit / delete the selected node | `n` | advance the planning gate |
| `a` / `j` | approve / reject the handover | `/` | search |

The agent registers the server as `specsmither` (the `init` scaffold writes it into
`.claude/settings.local.json`); every MCP response is TOON.

## Architecture

```
src/specsmither/
  domain/      enums, status transitions, runtime records (SpecFull recompose)
  db/          SQLAlchemy 2.0 ORM: base, models, migrations, *StoreSqlite repos
  dag/         status calc, single-hop cascade, dependency tree, critical path
  rollups/     count derivation, planning aggregate, in-txn recompute worklist
  operations/  CRUD, queries, search, reports, workspace/init, project, errors
  lifecycle/   planning verbs (start/action/complete) + handover, gate, state machine
  dispatch/    the single MCP routing facade (envelopes, error guidance)
  adapters/    WritePlan executor, crucible validator, lifecycle ports, config
  toon/        the TOON wire encoder (the always-on MCP encoding)
  mcp/         the stdio MCP server  → `specsmither-mcp`
  tui/         the Textual Observatory → `specsmither`
```

Every mutation runs in one `Session.begin()` (`BEGIN IMMEDIATE`) → apply the
WritePlan → recompute worklist (cascade → counts → tree, to fixpoint) →
commit/rollback. Derived data (counts, cached dependency tree) is materialized
in-transaction, recomputed **from the authoritative child rows** (no deltas,
race-free).

## Runtime & workspace

SpecSmither keeps everything in **one user-global SQLite database** — a single
`specsmither.db` that holds *every* project. A **workspace** is any directory
bound to one project via `.specsmither/config.json`; different workspaces serve
different projects off the same DB file (WAL + `BEGIN IMMEDIATE` make concurrent
workspaces/agents on one file safe).

```
~/.specsmither/specsmither.db          # the single DB — all projects live here
~/.specsmither/config.json             # global defaults
<workspace>/.specsmither/config.json   # { "projectId": "…", "specificationId"?, … }
```

| Env var | Purpose | Default |
|---|---|---|
| `SPECSMITHER_HOME` | SpecSmither home directory | `~/.specsmither` |
| `SPECSMITHER_DB` | Explicit DB file (overrides `HOME`) | `$SPECSMITHER_HOME/specsmither.db` |
| `SPECSMITHER_PROJECT` | Force the active project id (overrides the workspace file) | the workspace `config.json` |
| `SPECSMITHER_MCP_FORMAT` | MCP wire encoding (`toon` / `json`) | `toon` |
| `SPECSMITHER_LANGUAGE` | Guidance language (`en` / `pt-br`; aliases `pt`, `pt_BR`) | `en` |

Resolution precedence is **env > project (`.specsmither/config.json`) > global
(`~/.specsmither/config.json`) > default**.

## Development

```sh
uv sync --locked --dev
uv run ruff check src tests
uv run mypy
uv run pytest
```

Requires Python 3.11+. Determinism golden fixtures live in `fixtures/golden/` and Textual snapshot SVGs
in `tests/__snapshots__/`; both are committed (regenerated via `tools/` and
`pytest --snapshot-update`), so CI runs no Node.

## License

Apache-2.0.
