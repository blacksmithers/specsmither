"""The literal content emitted by ``init`` — skills, agents, hooks, statusline.

Kept as plain templates (one constant per artifact) so the emitter stays a thin
write-if-absent loop. The skills wrap the SpecSmither MCP tools (served by
``specsmither-mcp`` as ``mcp__specsmither__<tool>``, always TOON); the agents are
subagent defs; the hooks are read-only context injectors.
"""

from __future__ import annotations

SCAFFOLD_VERSION = "0.1.0"

_HEADER = f"<!-- SpecSmither v{SCAFFOLD_VERSION} -->"


def _skill(name: str, description: str, body: str) -> str:
    return f"{_HEADER}\n---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


def _agent(name: str, description: str, model: str, tools: str, body: str) -> str:
    return (
        f"{_HEADER}\n---\nname: {name}\ndescription: {description}\n"
        f"model: {model}\ntools: {tools}\n---\n\n{body.strip()}\n"
    )


# --------------------------------------------------------------------------- #
# skills (.claude/skills/<name>/SKILL.md) — wrap the MCP read tools            #
# --------------------------------------------------------------------------- #

SKILLS: dict[str, str] = {
    "ss-status": _skill(
        "ss-status",
        "Show the active SpecSmither spec's status — epics, tickets, and the readiness dashboard.",
        """
# ss-status (SpecSmither)

Render a consolidated status view for the active specification.

1. Read `.specsmither/config.json` for `projectId` / `specificationId` (if absent, tell the
   user to run the SpecSmither TUI and press `i` to init).
2. Call these `specsmither-mcp` tools (responses are TOON — render them as-is, do not re-serialize):
   - `mcp__specsmither__get` `{ "type": "specification", "id": <specificationId>, "summary": false }`
   - `mcp__specsmither__list` `{ "type": "epics", "specificationId": <specificationId> }`
   - `mcp__specsmither__get_report` `{ "report": "dashboard", "specificationId": <specificationId> }`
3. Summarize phase / progress / ready vs blocked. **Read-only** — never mutate here.
""",
    ),
    "ss-context": _skill(
        "ss-context",
        "View or switch the active SpecSmither project/specification for this workspace.",
        """
# ss-context (SpecSmither)

1. Read `.specsmither/config.json` → current `projectId` / `specificationId`.
2. `mcp__specsmither__list` `{ "type": "specifications", "projectId": <projectId> }` to list specs.
3. Report the active context. Switching the bound spec is a human action in the TUI (Browser → Enter).
""",
    ),
    "ss-next": _skill(
        "ss-next",
        "List the next actionable (ready) tickets for the active SpecSmither spec.",
        """
# ss-next (SpecSmither)

`mcp__specsmither__get_next_actionable_tickets` `{ "specificationId": <specificationId>, "limit": 10 }`
→ the ready tickets (deps satisfied). Pick one to implement. Read-only.
""",
    ),
    "ss-blockers": _skill(
        "ss-blockers",
        "Show blocked tickets, the blockers report, and the critical path.",
        """
# ss-blockers (SpecSmither)

- `mcp__specsmither__get_blocked_tickets` `{ "specificationId": <specificationId> }`
- `mcp__specsmither__get_report` `{ "report": "blockers", "specificationId": <specificationId> }`
- `mcp__specsmither__get_critical_path` `{ "specificationId": <specificationId> }`

Explain what unblocks the most work. Read-only.
""",
    ),
    "ss-search": _skill(
        "ss-search",
        "Search tickets in the active SpecSmither project by text / tags / status / complexity.",
        """
# ss-search (SpecSmither)

`mcp__specsmither__search` `{ "query": <text>, "projectId": <projectId> }`
(one scope + one filter required). Optional: `status`, `complexity`, `tags`, `specificationId`,
`epicId`. Return the ranked matches. Read-only.
""",
    ),
    "ss-tree": _skill(
        "ss-tree",
        "Show the dependency tree (DAG) for the active SpecSmither spec.",
        """
# ss-tree (SpecSmither)

`mcp__specsmither__get_dependency_tree` `{ "specificationId": <specificationId> }`
→ the materialized DAG (edges, ready/blocked, critical path, cycles). Read-only.
""",
    ),
    "ss-init": _skill(
        "ss-init",
        "Confirm the SpecSmither workspace is initialized and show its bound project.",
        """
# ss-init (SpecSmither)

1. Check for `.specsmither/config.json`. If missing, instruct the user to open the SpecSmither
   TUI (`specsmither`) and press `i` to initialize (creates the user-global DB + this workspace's
   project + this scaffold). Init is a human action — do not attempt to mutate the DB from here.
2. If present, confirm the bound `projectId` and offer `ss-status` / `ss-next` next.
""",
    ),
}


# --------------------------------------------------------------------------- #
# agents (.claude/agents/<name>.md)                                           #
# --------------------------------------------------------------------------- #

AGENTS: dict[str, str] = {
    "ssag-spec-creator": _agent(
        "ssag-spec-creator",
        "Author a SpecSmither specification end-to-end through the planning lifecycle.",
        "opus",
        "Read, Write, Edit, mcp__specsmither__*",
        """
# Spec creator (SpecSmither)

Drive a draft specification to `ready` through the planning gate, one phase at a time:

1. `mcp__specsmither__start_planning_session` `{ "specId": <id> }`.
2. Per phase, apply ONE operation at a time via `mcp__specsmither__action_planning_session`
   `{ "sessionId": <sid>, "operation": <op>, "payload": {...} }` until the gate passes
   (`agent_response.gate_result == "pass"`). Ops are phase-classified: a `native` op applies in
   place, a `late` op rewinds the session to its native phase (re-validate), a `forbidden` op is
   denied with guidance — read `agent_response.variant` / `.guidance`.
3. `mcp__specsmither__complete_planning_session` parks the phase for human review; the **human**
   approves the handover in the TUI (or the orchestrator calls `approve_handover`). Repeat through
   `cross_validation`; the terminal approve flips the spec to `ready`.
Never approve your own handover unless explicitly delegated.
""",
    ),
    "ssag-orchestrator": _agent(
        "ssag-orchestrator",
        "Plan work over a SpecSmither spec: read reports, delegate, own the handover decision.",
        "opus",
        "Read, Task, mcp__specsmither__*",
        """
# Orchestrator (SpecSmither)

- Read state with `get` / `list` / `get_report` / `get_next_actionable_tickets` /
  `get_blocked_tickets` / `get_critical_path`.
- Delegate spec authoring to `ssag-spec-creator` and implementation to `ssag-ticket-implementer`.
- Own the human-handover verbs when delegated: `mcp__specsmither__approve_handover`,
  `mcp__specsmither__reject_handover`, `mcp__specsmither__reject_handover_with_feedback`
  (`{ "sessionId": <sid>, "feedback": <text> }`). Approve only when the gate is passing.
""",
    ),
    "ssag-ticket-implementer": _agent(
        "ssag-ticket-implementer",
        "Implement a single ready SpecSmither ticket (work lifecycle lands in 0.2.0).",
        "sonnet",
        "Read, Write, Edit, Bash, mcp__specsmither__*",
        """
# Ticket implementer (SpecSmither)

Pick a ready ticket (`get_next_actionable_tickets`), read it (`get` `{ "type": "ticket" }`),
implement it against its acceptance criteria + steps, then link the PR with
`mcp__specsmither__link_pull_request`. The work-session gate (assay) ships in 0.2.0 — the
`*_work_session` tools are 0.1.0 stubs today.
""",
    ),
    "ssag-package-researcher": _agent(
        "ssag-package-researcher",
        "Research libraries/APIs for a SpecSmither ticket (web only — never mutates the engine).",
        "sonnet",
        "Read, WebSearch, WebFetch",
        """
# Package researcher (SpecSmither)

Investigate libraries / APIs / patterns relevant to a ticket and return a concise findings note
with citations. Web-only: this agent never calls a SpecSmither MCP tool and never mutates state.
""",
    ),
}


# --------------------------------------------------------------------------- #
# hooks + statusline (.specsmither/hooks/*.sh) — READ-ONLY context injectors   #
# --------------------------------------------------------------------------- #

HOOK_CONTEXT_SH = f"""#!/usr/bin/env bash
# {_HEADER}
# SpecSmither context hook (SessionStart / UserPromptSubmit). READ-ONLY, opt-in:
# it reads the workspace binding and injects a one-line context. It NEVER calls a
# mutating engine verb. Exits 0 quickly and is silent when the workspace is unbound.
set -euo pipefail
cfg=".specsmither/config.json"
[ -f "$cfg" ] || exit 0
# `|| true`: a missing key makes grep exit 1, which under pipefail+set -e would abort
# before the fallback echo — keep the hook silent + exit-0 for an unbound/partial config.
project=$(grep -o '"projectId"[^,}}]*' "$cfg" | head -1 | sed 's/.*: *"\\(.*\\)"/\\1/' || true)
spec=$(grep -o '"specificationId"[^,}}]*' "$cfg" | head -1 | sed 's/.*: *"\\(.*\\)"/\\1/' || true)
echo "SpecSmither: project=${{project:-?}} spec=${{spec:-none}}. Read via the ss-* skills /"
echo "specsmither-mcp (get/list/search/get_report); mutate only through the planning verbs."
"""

HOOK_STATUSLINE_SH = f"""#!/usr/bin/env bash
# {_HEADER}
# SpecSmither statusline (READ-ONLY). Prints the bound project/spec from the
# workspace config; never touches the engine.
set -euo pipefail
cfg=".specsmither/config.json"
[ -f "$cfg" ] || {{ echo "◆ SpecSmither (uninitialized)"; exit 0; }}
project=$(grep -o '"projectId"[^,}}]*' "$cfg" | head -1 | sed 's/.*: *"\\(.*\\)"/\\1/' || true)
spec=$(grep -o '"specificationId"[^,}}]*' "$cfg" | head -1 | sed 's/.*: *"\\(.*\\)"/\\1/' || true)
echo "◆ SpecSmither · project=${{project:-?}} · spec=${{spec:-none}}"
"""
