"""specsmither — local-first, NO-LLM reimplementation of the SpecForge engine.

Drives a software scope through ``Specification → Epic → Ticket → DAG`` over a
single SQLite file. Imports the two pure gates side-by-side: ``crucible`` (the
planning gate) and ``assay`` (the work gate, 0.2.0). The engine is fully
deterministic and synchronous; only the MCP server and the TUI are async
presentation adapters over it.

M0 (this layer): ``domain/``, ``db/``, ``dag/``, ``rollups/``, ``operations/``,
``adapters/`` — the deterministic substrate. No lifecycle, MCP, or CLI yet.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
