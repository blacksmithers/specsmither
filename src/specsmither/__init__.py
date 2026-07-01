"""specsmither — a local-first, NO-LLM control system for agentic AI implementation.

Drives a software scope through ``Specification → Epic → Ticket → DAG`` over a
single SQLite file, under two pure gates side-by-side: ``crucible`` (the planning
gate) and ``assay`` (the work gate, 0.2.0). The engine is fully deterministic and
synchronous; only the MCP server (``specsmither-mcp``) and the TUI (``specsmither``)
are async presentation adapters over it.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
