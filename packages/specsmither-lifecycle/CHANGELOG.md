# Changelog

All notable changes to `specsmither-lifecycle` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
adheres to [Semantic Versioning](https://semver.org/).

## 0.4.3

Closes a silent-no-op hole in the `action` pre-check chain: a planning payload with an
unrecognized key now denies instead of succeeding while writing nothing.

### Fixed

- **An unrecognized key in an `action` payload no longer silently no-ops.** The per-op
  `schema_validate` models used Pydantic's default `extra="ignore"`, so an unknown
  *top-level* key (a stray `type`, a typo, a payload wrapped under the wrong name) was
  dropped before the write. Separately, an `update_*` `fields` map is typed
  `dict[str, Any]`, so an unknown key *inside* it (a typo like `gaols`, a double-wrap
  `{fields: {fields: {…}}}`) was snake-cased and `setattr`'d onto the `extra="allow"`
  crucible model as a stray attribute while the real field stayed untouched. Both paths
  reported `success` while writing nothing, the gate never moved, and the agent got no
  corrective signal — the loop that pinned a spec at a stuck score. Two changes close
  the hole: the `_Payload` base now uses `extra="forbid"` (the create ops gained the
  optional `id` / `content` wire fields the projector already reads, so they still
  validate), and a new `unknown_field_key` pre-check snake-cases each `fields` key and
  denies any that is not a declared field of the target crucible model. A mis-shaped
  payload is now a `Denied(invalid_payload)` that names the offending key; the
  `fieldDeclarations` agent-wire field (stripped before the checks run) is unaffected.

## 0.4.2

Fixes the counterpart gate-consistency bug on the mutating path: `action_planning_session`
now reports the same spec-wide verdict as completion and the status poll.

### Fixed

- **`action_planning_session` no longer contradicts `complete_planning_session`.** The
  verb's agent-facing verdict, its persisted `last_gate_result`, and the guidance's
  next-entity list were derived from `evaluate_phase_gate` (touched-subset scope). At
  the two `*_expansion` phases that gate scores only the entity the operation touched,
  so an edit that cleared the touched epic could report `gate_passed` while another epic
  still sat below threshold — a state `complete_planning_session` and the 0.4.1-fixed
  `get_planning_status` both call "fail". That trapped an agent in an edit→complete loop
  and persisted a misleading `last_gate_result="pass"` for every downstream consumer
  (the handover/approve precondition, the UI gate badge, the status-poll cache
  fallback). The verdict, the persisted gate, and the next-entity list now come from
  `evaluate_phase_gate_spec_wide` — the same spec-wide all-pass scope completion and the
  status poll use — so the three verbs never disagree. The touched-scoped gate is
  retained only for the per-entity score datapoints, so a score is still stamped just
  for the entity the operation touched, not a spurious full refresh of every entity on
  every edit.

## 0.4.1

Fixes a gate-consistency bug between the read-only status poll and completion.

### Fixed

- **`get_planning_status` no longer contradicts `complete_planning_session`.** The
  read-only status poll composed the gate verdict from the cached
  `session.last_gate_result`, while completion always re-validates live — so a spec
  whose `local_score` reached the threshold but violated the structural floor (e.g.
  `goals`/`requirements` with fewer than three items) could report "gate pass" on the
  poll yet be denied by completion, trapping an agent in a status→complete loop. The
  poll now re-validates live for the `PHASE_STATUS_REPORT` variant, routing through the
  same `evaluate_phase_gate_spec_wide` the completion gate uses, so the two agree
  unconditionally — over a stale cache, after a reject re-activates a session, or
  against an out-of-band edit. The re-validation is display-only: no fresh gate cache is
  persisted, and every other poll variant (including the `human_feedback_received`
  synthetic move) keeps composing from cached state unchanged.

## 0.4.0

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
  translation. An embed selects the language per project/spec via the `config_store`
  (`guidance.language`) or as an ambient default via `LifecyclePorts.default_language`;
  the resolved tag is forwarded to `crucible-forge` so its findings match.
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

### Fixed

- **`human_feedback_received` status poll** now carries a recommended move (the phase's
  first native op, "apply the human's feedback") instead of an empty list.

### Removed

- **`inspect_planning_session`** — an unreachable verb no surface dispatched. The
  read-only status report is the `get_planning_status` operation of
  `action_planning_session`, which is fully retained.

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
