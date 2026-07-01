"""The SpecSmither Textual TUI Observatory (planner/02) — the only human surface.

A workspace-scoped, in-process interactive surface over the one dispatch facade +
the operations layer: ``init`` (bootstrap), browse · DAG · monitor planning ·
approve/reject handover · search · full CRUD. No web, no CLI; the agent drives the
same DB via ``specsmither-mcp`` (TOON) in parallel.

The launcher lives in :mod:`specsmither.tui.app` (``from specsmither.tui.app import
run``) — imported lazily so ``import specsmither.tui.data`` (the binding seam, unit
tested without Textual mounted) does not force the whole UI stack.
"""

from __future__ import annotations

__all__ = ["run"]


def run() -> None:
    """Open the Observatory (thin re-export of :func:`specsmither.tui.app.run`)."""
    from specsmither.tui.app import run as _run

    _run()
