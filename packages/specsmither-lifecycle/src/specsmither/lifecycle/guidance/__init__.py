"""Planning-lifecycle guidance composers (the agent-facing prose layer).

A **minimal, English-first** guidance layer. In place of a large composer family,
0.1.0 ships one clean composer that produces the :class:`PlanningAgentResponse` every
verb returns; the rich per-field interview prose is deferred (architecture §6 — enrich
later). Submodules are imported directly (``from specsmither.lifecycle.guidance.compose
import compose_response``); this package root is intentionally bare.
"""
