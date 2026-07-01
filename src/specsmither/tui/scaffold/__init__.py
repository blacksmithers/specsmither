"""Claude Code scaffolding emitted by ``init`` (planner/02 #11–#14).

``init`` writes an agent-facing toolkit into the workspace's ``.claude/`` (skills
that wrap the MCP tools, subagent defs, opt-in hooks + a statusline) so the agent
can drive the *same* engine the human drives in the TUI. Everything here is:

* **idempotent** — a file already present is left untouched (re-running ``init`` is
  a no-op); ``settings.local.json`` is *merged*, never clobbered.
* **read-only w.r.t. the engine** — the hooks only inject context (they read the
  workspace ``config.json``); they never call a mutating verb.

:func:`plan_items` previews what ``init`` would write (for the InitModal);
:func:`emit` actually writes it. Both return ``(path, note, status)`` rows where
``status ∈ {created, exists, pending}``.
"""

from __future__ import annotations

from specsmither.tui.scaffold.emitter import SCAFFOLD_VERSION, emit, plan_items

__all__ = ["SCAFFOLD_VERSION", "emit", "plan_items"]
