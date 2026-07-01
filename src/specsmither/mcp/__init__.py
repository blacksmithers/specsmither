"""The L6 async MCP adapter — the stdio server over the sync dispatch facade.

This package is the *only* async layer in SpecSmither. It is a thin protocol shell:
:mod:`specsmither.mcp.server` advertises the 21 tools (the 18
:data:`~specsmither.dispatch.facade.TOOL_NAMES` plus the 3
:data:`~specsmither.dispatch.facade.HANDOVER_TOOL_NAMES`) and forwards every call to the
synchronous :class:`~specsmither.dispatch.facade.Dispatcher`, TOON-encoding the JSON-able
content it hands back. The engine stays sync; the loop never blocks on it (the sync
``dispatch`` runs on a worker thread via ``anyio.to_thread``).
"""

from __future__ import annotations

__all__: list[str] = []
