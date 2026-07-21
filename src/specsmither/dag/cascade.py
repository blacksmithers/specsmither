"""Single-hop cascade engine (pure).

Covers the on-completed / on-uncompleted / on-dependency-change transitions plus
the find-dependents and dependency-checker helpers.

A transitive chain-cascade is DROPPED on purpose: promotion is single-level
(linear A→B→C with only A done promotes ONLY B — C is *not* reached), and the
only cycle guard it needed is subsumed by the tree's Tarjan SCC.

Why single-hop is complete: completing X can ready only X's *direct* dependents;
a ticket going ``ready`` (≠ ``done``) cannot unblock anyone, so there is no
transitive ready-cascade to propagate. The recompute worklist re-fires the
single-hop variants per applied transition instead.

Determinism notes:
- ``states.get(id)`` returning ``None`` for an unknown id flows through the
  ``!= 'done'`` / ``not in {…}`` guards, so an unknown id is treated consistently.
- ``check_dependencies_completed`` counts an UNKNOWN dependency id (absent from
  ``states``) as BOTH a blocker and missing. Callers pass the FULL spec
  neighborhood, so this never falsely blocks in practice — it is kept for safety.
- ``find_dependents`` / ``find_dependencies`` dedup and preserve first-seen
  (insertion) order from the linear edge scan.
- Reason strings (``'deps-completed'`` / ``'dep-reverted'``) are fixed literals so
  transition lists compare cleanly against the golden fixtures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from specsmither.dag.types import DepEdge

__all__ = [
    "CascadeTransition",
    "DependencyCheckResult",
    "cascade_on_completed",
    "cascade_on_dependency_change",
    "cascade_on_uncompleted",
    "check_dependencies_completed",
    "find_dependencies",
    "find_dependents",
]


@dataclass(frozen=True, slots=True)
class CascadeTransition:
    """A single ticket status change proposed by the cascade engine.

    ``from``/``to`` are spelled as the non-keyword ``from_status``/``to_status``.
    ``reason`` is one of the fixed literals (``'deps-completed'`` or
    ``'dep-reverted'``).
    """

    ticket_id: str
    from_status: str
    to_status: str
    reason: str


@dataclass(frozen=True, slots=True)
class DependencyCheckResult:
    """Result of ``check_dependencies_completed``.

    ``all_completed`` is true iff every dependency is ``done``. An unknown
    dependency id appears in BOTH ``blockers`` and ``missing``.
    """

    all_completed: bool
    blockers: list[str]
    missing: list[str]


def find_dependents(edges: Sequence[DepEdge], ticket_id: str) -> list[str]:
    """Tickets whose ``depends_on_id == ticket_id`` (deduped, first-seen order)."""
    out: list[str] = []
    seen: set[str] = set()
    for edge in edges:
        if edge.depends_on_id == ticket_id and edge.ticket_id not in seen:
            seen.add(edge.ticket_id)
            out.append(edge.ticket_id)
    return out


def find_dependencies(edges: Sequence[DepEdge], ticket_id: str) -> list[str]:
    """The ticket's own ``depends_on_id`` targets (deduped, first-seen order)."""
    out: list[str] = []
    seen: set[str] = set()
    for edge in edges:
        if edge.ticket_id == ticket_id and edge.depends_on_id not in seen:
            seen.add(edge.depends_on_id)
            out.append(edge.depends_on_id)
    return out


def check_dependencies_completed(
    ticket_id: str,
    states: Mapping[str, str],
    edges: Sequence[DepEdge],
) -> DependencyCheckResult:
    """Whether every dependency of ``ticket_id`` is ``done``.

    No dependencies → vacuously completed. An UNKNOWN dependency id (absent from
    ``states``) counts as both a blocker and missing.
    """
    dep_ids = find_dependencies(edges, ticket_id)
    if not dep_ids:
        return DependencyCheckResult(all_completed=True, blockers=[], missing=[])

    blockers: list[str] = []
    missing: list[str] = []
    for dep_id in dep_ids:
        status = states.get(dep_id)
        if status is None:
            missing.append(dep_id)
            blockers.append(dep_id)
            continue
        if status != "done":
            blockers.append(dep_id)

    return DependencyCheckResult(
        all_completed=len(blockers) == 0,
        blockers=blockers,
        missing=missing,
    )


def cascade_on_completed(
    states: Mapping[str, str],
    edges: Sequence[DepEdge],
    changed_id: str,
) -> list[CascadeTransition]:
    """Root went ``done``: promote each ``pending`` direct dependent whose deps
    are ALL ``done`` (``pending → ready``). Single-hop only."""
    transitions: list[CascadeTransition] = []
    if states.get(changed_id) != "done":
        return transitions

    for dep_id in find_dependents(edges, changed_id):
        if states.get(dep_id) != "pending":
            continue
        if not check_dependencies_completed(dep_id, states, edges).all_completed:
            continue
        transitions.append(
            CascadeTransition(
                ticket_id=dep_id,
                from_status="pending",
                to_status="ready",
                reason="deps-completed",
            )
        )
    return transitions


def cascade_on_uncompleted(
    states: Mapping[str, str],
    edges: Sequence[DepEdge],
    changed_id: str,
) -> list[CascadeTransition]:
    """Root left ``done``: unconditionally demote each ``ready`` direct dependent
    (``ready → pending``). ``active``/``done`` dependents are never re-blocked."""
    transitions: list[CascadeTransition] = []
    if states.get(changed_id) == "done":
        return transitions

    for dep_id in find_dependents(edges, changed_id):
        if states.get(dep_id) != "ready":
            continue
        transitions.append(
            CascadeTransition(
                ticket_id=dep_id,
                from_status="ready",
                to_status="pending",
                reason="dep-reverted",
            )
        )
    return transitions


def cascade_on_dependency_change(
    states: Mapping[str, str],
    edges: Sequence[DepEdge],
    changed_id: str,
) -> list[CascadeTransition]:
    """A dependency EDGE on ``changed_id`` was added/removed: re-derive only that
    ticket's blockedness. ``pending`` & all deps ``done`` → ``ready``; ``ready`` &
    not all done → ``pending``. ``active``/``done`` are never re-blocked."""
    transitions: list[CascadeTransition] = []
    status = states.get(changed_id)
    if status not in ("pending", "ready"):
        return transitions

    all_completed = check_dependencies_completed(changed_id, states, edges).all_completed
    if status == "pending" and all_completed:
        transitions.append(
            CascadeTransition(
                ticket_id=changed_id,
                from_status="pending",
                to_status="ready",
                reason="deps-completed",
            )
        )
    elif status == "ready" and not all_completed:
        transitions.append(
            CascadeTransition(
                ticket_id=changed_id,
                from_status="ready",
                to_status="pending",
                reason="dep-reverted",
            )
        )
    return transitions
