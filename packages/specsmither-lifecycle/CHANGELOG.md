# Changelog

All notable changes to `specsmither-lifecycle` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
adheres to [Semantic Versioning](https://semver.org/).

## 0.3.0

Adds a guidance internationalization layer and two structured recovery plans for the
cross-validation denials, and tracks the `crucible-forge` 0.4.0 engine.

### Added

- **`specsmither.lifecycle.i18n`** — a keyed English catalog (`TEXT_EN`) that a
  `data/<lang>.json` overlay translates per key (per-key English fallback), sharing
  crucible's language-tag semantics and `{placeholder}` substitution. `resolve_language`
  reads the normalized `guidance.language` from the resolved lifecycle config and falls
  back to `en` for an absent/unsupported value. Every composer body and the per-field
  instruction scaffolding resolve through it; the field catalogs gain per-language
  `catalogs/<phase>.<lang>.yaml` prose overlays. Ships a complete Brazilian-Portuguese
  translation.
- **`cycle_guidance.format_cycle_analysis` / `build_cycle_detected_denial`** — the
  per-edge evidence + 2-D recovery packet (cycle shape × the spurious edge's
  intra-batch/persisted origin) that enriches the `cycle_detected` deny.
- **`creator_plan_guidance.format_creator_plan`** — the consolidated creator-election
  plan prepended to a cross-validation file-provenance `gate_not_passed` deny.

### Changed

- **Requires `crucible-forge >= 0.4.0`.** `Validator.validate` gains a `language`
  argument, threaded from the resolved guidance language through APS / CPS / SPS so the
  engine emits findings in the same language as the composer.
- **N/A declarations point at the `justify` op.** The rendered per-field N/A
  instruction (and the cross-validation N/A prose) now name the first-class `justify`
  operation and the bare N/A scope, replacing the retired `update_*`-with-
  `fieldDeclarations` path.
- **`planning-lifecycle` config schema version 3** — adds `guidance.language`.

## 0.2.0

Tracks the `crucible-forge` 0.3.0 validation engine and adds a catalog-parity
contract so any tool surface can be tested against the deny-gate it must conform to.

### Added

- **`PLANNING_OPERATION_CONTRACT`** — a plain-data view of the per-operation
  deny-gate (`op → required payload fields`, plus the required fields of a payload's
  array-of-objects field). Derived directly from the payload models, so it cannot
  drift from what the gate enforces.
- **`check_planning_catalog_parity()`** + **`catalog_branches_from_oneof()`** — a
  reusable guard that asserts a public tool catalog's `action_planning_session`
  `oneOf` advertises exactly the shapes the gate accepts. A catalog that offers, say,
  `epicId` where the gate requires `id`, or `{ticketId, dependsOnId}` where it
  requires `{fromTicketId, toTicketId}`, is flagged before it can deny a
  spec-following agent at runtime.

### Changed

- **Requires `crucible-forge >= 0.3.0`.** The cross-validation guidance now reflects
  the engine's current registry: ten checks run in the `cross_validation` phase,
  file coordination is covered by `file-provenance` and `concurrent-modification`,
  and blueprint coverage is gated earlier (in `ticket_decomposition`). The
  `create_dependencies` batch cap is stated as 5000 edges per call.

## 0.1.0

First release — the pure planning-lifecycle core, extracted from the `specsmither`
product into its own publishable distribution with **no persistence and no UI/server
dependency** (no SQLAlchemy, no Textual, no MCP). A host embeds the planning state
machine in-process and provides its own persistence through the `WritePlan` seam.

### Added

- **Pure verb surface** — `start_planning_session`, `action_planning_session`,
  `complete_planning_session`, `inspect_planning_session`, and the handover verbs
  (`approve_handover` / `reject_handover` / `reject_handover_with_feedback`). Each is
  a pure `(payload, ports) -> {response, WritePlan}` function: it reads state through
  injected ports and returns the mutation as a `WritePlan` — it performs no I/O.
- **`LifecyclePorts`** — the injection contract: the `PlanningSessionStore` /
  `SpecStore` / `ConfigStore` read ports, the `Validator` (crucible), the pure
  operations projector, and the optional `persist_write_plan` / `clock` /
  `id_generator`. The host implements the stores over its own database.
- **`WritePlan` data contract** (`lifecycle.write_plan_types`) — an ordered list of
  serializable item kinds (`specMutation`, `sessionUpdate`, `actionAppend`,
  score/transition datapoints, …). Any executor — the product's SQLite one or an
  external Postgres one — consumes it as plain data.
- **`PlanningSessionRecord`** — a pure, DB-free frozen record for the session state
  the verbs read/return (replaces the ORM row on the verb boundary).
- **In-memory operations projector** — computes post-mutation state for the
  validator/gate without touching a database.
- **Phase-gate evaluator + guidance composer + prechecks** — the deterministic gate
  and the agent-facing guidance, driven entirely by `crucible.validate`.

### Dependencies

`crucible-forge`, `pydantic`, `python-ulid`, `pyyaml` only. This distribution shares
the `specsmither` PEP 420 namespace with the `specsmither` product distribution; both
build and publish independently from the same repository.
