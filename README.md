# SpecSmither

Open-source, local-first, **NO-LLM** reimplementation of the SpecForge engine.

SpecSmither drives a software scope through `Specification → Epic → Ticket → DAG`
and two lifecycles — **planning** (authoring a spec under a scoring gate) and
**work** (building each ticket under a completion gate) — entirely over a single
SQLite file. The engine is fully deterministic and synchronous; all AI reasoning
lives in the MCP *client*, never in the engine.

It imports two independent, pure gates side-by-side:

- **[crucible](https://github.com/blacksmithers/crucible)** (`crucible-forge`) — the
  planning gate (rubric scorer). A published dependency from day one.
- **assay** (`assayforge → assay`) — the work-session completion gate (0.2.0).

## Status

Pre-alpha. Building **M0 — Foundation**: the deterministic substrate (domain
models, SQLite/ORM persistence, the DAG engine, the in-transaction recompute
worklist, CRUD primitives, and the query/search/report surface) with native
tests. No lifecycle, MCP server, or CLI yet.

## Architecture (M0 layers)

```
src/specsmither/
  domain/      enums, status transitions, runtime records (SpecFull recompose)
  db/          SQLAlchemy 2.0 ORM: base, models, migrations, *StoreSqlite repos
  dag/         status calc, single-hop cascade, dependency tree, critical path
  rollups/     count derivation, planning aggregate, in-txn recompute worklist
  operations/  CRUD, queries, search, reports, lookup, errors, reopen, link-PR
  adapters/    WritePlan executor, in-memory operations projector
```

Every mutation runs in one `Session.begin()` (`BEGIN IMMEDIATE`) → apply the
WritePlan → recompute worklist (cascade → counts → tree, to fixpoint) →
commit/rollback. Derived data (counts, cached dependency tree) is materialized
in-transaction, recomputed **from the authoritative child rows** (no deltas,
race-free).

## Development

```sh
uv sync --dev          # resolves crucible-forge from ../crucible (see pyproject)
uv run ruff check src tests
uv run mypy
uv run pytest
```

Requires Python 3.11+. Determinism golden fixtures live in `fixtures/golden/`
and are committed (regenerated from the TS reference via `tools/`); CI runs no
Node.

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

Running `init` in a workspace (once) lazily creates the DB if it's absent, then
creates — or reuses — that workspace's project and writes the binding file.

| Env var | Purpose | Default |
|---|---|---|
| `SPECSMITHER_HOME` | SpecSmither home directory | `~/.specsmither` |
| `SPECSMITHER_DB` | Explicit DB file (overrides `HOME`) | `$SPECSMITHER_HOME/specsmither.db` |
| `SPECSMITHER_PROJECT` | Force the active project id (overrides the workspace file) | the workspace `config.json` |
| `SPECSMITHER_MCP_FORMAT` | MCP wire encoding (`toon` / `json`) | `toon` |

Resolution precedence is **env > project (`.specsmither/config.json`) > global
(`~/.specsmither/config.json`) > default**.

## License

Apache-2.0.
