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

## License

Apache-2.0.
