# Changelog

All notable changes to `specsmither` are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/); this project
adheres to [Semantic Versioning](https://semver.org/).

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
