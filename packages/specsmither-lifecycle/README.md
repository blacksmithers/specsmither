# specsmither-lifecycle

The **pure** planning-lifecycle core of [SpecSmither](https://github.com/blacksmithers/specsmither).

This distribution contributes the `specsmither.lifecycle`, `specsmither.domain`, and
`specsmither.ids` modules to the shared `specsmither` PEP 420 namespace. It is the
crucible-gated verb surface — `start` / `action` / `complete` / `inspect` plus the
handover verbs — together with the in-memory operations projector, the `WritePlan`
data contract, and the domain records/enums.

It is deliberately **dependency-light**: `crucible-forge`, `pydantic`, `python-ulid`,
and `pyyaml` only. It pulls in **no** SQLAlchemy, Textual, or MCP — those live in the
`specsmither` product distribution, which depends on this package and wires it to a
concrete SQLite store, TUI, and MCP server.

The verb surface satisfies a pure `(payload, ports) -> {response, WritePlan}` contract:
a host embeds it by providing the `LifecyclePorts` seam (stores, validator, operations
projector, clock, id generator) and consuming the returned `WritePlan`.
