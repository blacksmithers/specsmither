# Changelog

All notable changes to `specsmither` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
adheres to [Semantic Versioning](https://semver.org/).

## 0.4.2

Fixes the counterpart gate-consistency bug on the mutating path.
Requires `specsmither-lifecycle >= 0.4.2`.

### Fixed

- **`action_planning_session` no longer contradicts `complete_planning_session`.** After
  0.4.1 aligned the read-only status poll with completion, the same contradiction still
  lived on the mutating path: `action`'s verdict and its persisted `last_gate_result`
  came from the touched-subset gate, so at the `epic_expansion` / `ticket_expansion`
  phases an edit that cleared the touched entity could report "gate pass" while another
  entity sat below threshold — the state completion and the status poll both deny. That
  trapped an agent in an edit→complete loop and left a misleading `last_gate_result="pass"`
  for the handover precondition, the UI gate badge, and the status-poll cache. `action`
  now reports the spec-wide all-pass verdict (the scope completion and the poll use), so
  the three verbs always agree; the touched-subset gate is kept only for the per-entity
  score datapoints, so a score is still stamped just for the entity the edit touched.

## 0.4.1

Fixes a gate-consistency bug between the read-only status poll and completion.
Requires `specsmither-lifecycle >= 0.4.1`.

### Fixed

- **`get_planning_status` no longer contradicts `complete_planning_session`.** The
  read-only status poll reported the gate from the cached `session.last_gate_result`
  while completion always re-validates live, so a spec whose score cleared the threshold
  but violated the structural floor (e.g. fewer than three `goals`/`requirements`) could
  show "gate pass" on the poll yet be denied by completion — trapping an agent in a
  status→complete loop. The poll now re-validates live for the active `PHASE_STATUS_REPORT`
  view, routing through the same phase-gate evaluator completion uses, so the two verdicts
  always agree (stale cache, post-reject re-activation, or out-of-band edits). Display-only:
  no fresh gate cache is persisted.

## 0.4.0

Brings the planning guidance to Brazilian Portuguese and turns two cross-validation
denials that used to leave an agent guessing into concrete, batched recovery plans.
Adopts the `crucible-forge` 0.4.0 engine.

### Added

- **Guidance internationalization.** Set `SPECSMITHER_LANGUAGE=pt-br` (aliases
  `pt` / `pt_BR`), or per project via the `planning-lifecycle` `guidance.language`
  config, to get the
  entire planning guidance surface in Brazilian Portuguese — the variant bodies, the
  per-field interview catalogs, and the two structural recovery plans below, plus the
  validator engine's own findings. Scores, field paths, operation names, and code
  examples stay canonical, so an agent follows the same payloads in either language.
  The default (`en`) output is byte-for-byte unchanged. An unsupported tag falls back
  to English rather than failing the session.
- **Cycle-resolution guidance.** When declaring dependencies would close a cycle, the
  denial now carries a per-edge evidence packet (which direction is file-backed, the
  epic/ticket order, the natural-precedence hint) plus a per-edge recovery plan — drop
  the spurious edge by resubmitting without it when it is new to the batch, or by
  `delete_dependencies` when it is already persisted; reshape the files when the loop
  is a genuine circular file dependency. Indicative, never auto-applied.
- **Creator-election plan on the shared-file gate.** When the cross-validation gate
  denies on file provenance, it now leads with a consolidated plan: for every shared
  file that tickets touch but none creates, the elected creator (the earliest
  toucher), the exact dependency edges the other touchers need, and a per-file
  acyclicity verdict — framed as ONE structural batch (elect creators, then declare
  deps) instead of a per-file patch-and-rediscover loop.

### Changed

- **Adopts `crucible-forge` 0.4.0.** The validator engine gains guidance
  internationalization; the adapter forwards the resolved language so engine findings
  match the composer's. Default-English output is unchanged.

## 0.3.0

Adopts the `crucible-forge` 0.3.0 validation engine — including its new file-graph
coordination checks — and advertises the planning tool's per-operation payload shapes
on the MCP surface.

### Added

- **Real-repo grep evidence for the validator.** crucible 0.3.0 adds a
  `file-provenance` check (a consumed path must exist — be created in-spec or present
  in the repository) and a `concurrent-modification` check (two tickets that modify the
  same file must be ordered by a dependency path). The validator adapter now supplies
  the evidence these need: it computes the candidate paths crucible asks about and
  probes the project working tree, passing the set that exists as `existingFiles`.
  A `filesystem_file_prober` rooted at the workspace is wired through
  `make_dispatcher(project_root=…)` (and the MCP server / TUI resolve that root via the
  new `resolve_workspace_root`); with no root the engine falls back to strict
  spec-internal existence.
- **Per-operation MCP catalog.** `action_planning_session` now advertises a `oneOf`
  over the fifteen planning operations, each branch carrying its payload shape, so an
  agent sees the exact fields an operation needs. A parity test couples this catalog to
  the lifecycle deny-gate — the public surface can never drift from what the gate
  accepts.

### Changed

- **Requires `crucible-forge >= 0.3.0`** (and `specsmither-lifecycle >= 0.2.0`). The
  cross-validation adapter maps the engine's current operation vocabulary
  (`create_dependencies`, `delete_dependencies`, `link_blueprint_to_tickets`,
  `unlink_blueprint_to_tickets`); the cross-validation guidance reflects the ten checks
  that run in the terminal phase.

### Notes

- The stricter file-provenance gate is a behavior change: a spec that modifies files
  which are neither created in-spec nor present in the working tree — with no grep
  evidence supplied — is now flagged. Run the planner in the project root (where the
  MCP server and TUI resolve their working tree) so the evidence is available.
