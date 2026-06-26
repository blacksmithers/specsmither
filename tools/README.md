# tools/ — determinism golden generators (maintainer-only)

These scripts run the **TypeScript** SpecForge DAG engine (via `tsx`) to emit the
committed parity fixtures under `../fixtures/golden/`. CI runs **no Node** — the
golden fixtures are committed and consumed by the native pytest suite; these
scripts only regenerate them.

```
SPECFORGE=/opt/source/specforge npx tsx tools/gen_critical_path_golden.mjs
```

Populated by M0 work item #24 (DAG layer). Each generator imports the relevant
`@specforge/operations` aggregator and writes its `{input, expected}` cases as
JSON into `fixtures/golden/`.
