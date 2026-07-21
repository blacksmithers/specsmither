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

## Guidance language

All guidance prose is internationalized (English default, Brazilian Portuguese
included). An embed selects the language two ways, in precedence order:

1. **Per project / per spec** — return `{"guidance": {"language": "pt-br"}}` from the
   `config_store` for the `planning-lifecycle` domain (frozen into the spec snapshot).
2. **Ambient default** — set `LifecyclePorts(default_language="pt-br", …)`; used when a
   project sets no explicit language. Supported: `en`, `pt-br` (aliases `pt`, `pt_BR`);
   an unsupported tag degrades to `en`. The same tag is forwarded to `crucible-forge`
   so its validator findings match.
