"""The four agent-facing planning verbs (``start`` / ``action`` / ``complete`` /
``inspect``).

Each verb is a **pure** function ``(payload, ports) -> VerbResult`` — it reads via the
:class:`~specsmither.lifecycle.ports.LifecyclePorts` seam, runs the deterministic
pipeline (pre-checks → projector → validator → gate), and *builds* a
:class:`~specsmither.adapters.write_plan_executor.WritePlan` plus the composed
:class:`~specsmither.lifecycle.guidance.types.PlanningAgentResponse`; it never writes.
:func:`~specsmither.lifecycle.dispatch.create_lifecycle` persists the plan.

Submodules are imported directly (``from specsmither.lifecycle.verbs.start import
start_planning_session``); this package root is intentionally bare.
"""
