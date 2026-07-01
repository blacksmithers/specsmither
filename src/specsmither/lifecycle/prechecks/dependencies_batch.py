"""Pre-check: validate a ``create_dependencies`` batch before any write.

Faithful port of ``planning/pre-checks/dependencies-batch-validation.ts`` (M7.7,
A1 §1.4) + the APS-side denial mapping (action-planning-session.ts:171-207). Pure.

:func:`run_dependencies_batch` runs three sequential checks over the incoming
batch and returns the rich :data:`BatchValidationResult` the L4 pipeline persists
from (``to_persist`` are the survivors):

1. **intra-batch dedup** — drop exact-match duplicates within ``incoming``.
2. **persisted-overlap dedup** — drop edges already present in ``existing``.
3. **cycle detection** — seed the graph with ``existing`` (assumed acyclic) and
   add each surviving candidate only if it does not close a directed cycle;
   report the EXACT cycle path (the closing edge + the back-path), not just
   "a cycle exists". A cycle-closing edge is **not** added, keeping the baseline
   clean for later candidates.

:func:`validate_dependencies_batch` is the homogeneous pre-check wrapper the APS
chain calls: it maps the rich result onto :class:`Accepted` | :class:`Denied`
(``cycle_detected`` if any cycle, else ``batch_fully_deduped`` if nothing
survives dedup, else accepted).

The cross-existing cycle uses the TS incremental ``findPath`` walk rather than the
shared :func:`specsmither.dag.critical_path.find_cycles` SCC pass: only the
incremental walk can drop the single offending edge and report the closing edge
per cycle (SCC over the merged set cannot attribute a cycle to a specific
incoming edge, nor keep the acyclic survivors distinct).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from specsmither.lifecycle.ports import SpecDependencyEdge, TicketRef
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = [
    "BatchValidationCycle",
    "BatchValidationOk",
    "BatchValidationResult",
    "DetectedCycle",
    "run_dependencies_batch",
    "validate_dependencies_batch",
]


@dataclass(frozen=True)
class DetectedCycle:
    """A cycle closed by one incoming edge (``DetectedCycle``, lines 27-32).

    ``new_edge`` is the incoming edge whose addition closes the cycle;
    ``cycle_path`` is the full cycle as an ordered edge list, starting with
    ``new_edge``.
    """

    new_edge: SpecDependencyEdge
    cycle_path: list[SpecDependencyEdge]


@dataclass(frozen=True)
class BatchValidationOk:
    """No cycle — ``to_persist`` are the survivors after dedup (``ok: true`` branch)."""

    to_persist: list[SpecDependencyEdge]
    dropped_intra_batch: list[SpecDependencyEdge]
    dropped_persisted: list[SpecDependencyEdge]


@dataclass(frozen=True)
class BatchValidationCycle:
    """One or more cycles detected (``ok: false, reason: 'CYCLE_DETECTED'`` branch)."""

    cycles: list[DetectedCycle]
    dropped_intra_batch: list[SpecDependencyEdge]
    dropped_persisted: list[SpecDependencyEdge]


#: The rich result of :func:`run_dependencies_batch`.
BatchValidationResult = BatchValidationOk | BatchValidationCycle


def _edge_key(edge: SpecDependencyEdge) -> str:
    return f"{edge.from_ticket_id} {edge.to_ticket_id}"


def _find_path(
    start: str, goal: str, adjacency: dict[str, list[str]]
) -> list[SpecDependencyEdge] | None:
    """Find a directed path ``start ⇝ goal`` over ``adjacency`` (``findPath``).

    Iterative DFS tracking the edge trail to reconstruct the path. Returns the
    ordered edge list, or ``None`` if ``goal`` is unreachable from ``start``.
    """

    stack: list[tuple[str, list[SpecDependencyEdge]]] = [(start, [])]
    visited: set[str] = set()
    while stack:
        node, trail = stack.pop()
        if node == goal and trail:
            return trail
        if node in visited:
            continue
        visited.add(node)
        for nxt in adjacency.get(node, []):
            stack.append(
                (nxt, [*trail, SpecDependencyEdge(from_ticket_id=node, to_ticket_id=nxt)])
            )
    return None


def run_dependencies_batch(
    incoming: Sequence[SpecDependencyEdge],
    existing: Sequence[SpecDependencyEdge],
    tickets: Sequence[TicketRef] = (),
) -> BatchValidationResult:
    """Dedup + cycle-check ``incoming`` against ``existing`` (``validateDependenciesBatch``).

    ``tickets`` is reserved for caller-side ticket-existence guards (unused here,
    matching the source's I/O-free contract).
    """

    del tickets  # reserved; the algorithm is graph-only (parity with the TS)

    # ---- 1. intra-batch dedup --------------------------------------------- #
    seen: set[str] = set()
    dropped_intra_batch: list[SpecDependencyEdge] = []
    after_intra: list[SpecDependencyEdge] = []
    for edge in incoming:
        key = _edge_key(edge)
        if key in seen:
            dropped_intra_batch.append(edge)
            continue
        seen.add(key)
        after_intra.append(edge)

    # ---- 2. persisted-overlap dedup --------------------------------------- #
    existing_keys = {_edge_key(e) for e in existing}
    dropped_persisted: list[SpecDependencyEdge] = []
    candidates: list[SpecDependencyEdge] = []
    for edge in after_intra:
        if _edge_key(edge) in existing_keys:
            dropped_persisted.append(edge)
            continue
        candidates.append(edge)

    # ---- 3. cycle detection ----------------------------------------------- #
    adjacency: dict[str, list[str]] = {}
    for edge in existing:
        adjacency.setdefault(edge.from_ticket_id, []).append(edge.to_ticket_id)

    cycles: list[DetectedCycle] = []
    to_persist: list[SpecDependencyEdge] = []
    for edge in candidates:
        # Adding from→to closes a cycle iff `to` can already reach `from`.
        back = _find_path(edge.to_ticket_id, edge.from_ticket_id, adjacency)
        if back is not None:
            cycles.append(DetectedCycle(new_edge=edge, cycle_path=[edge, *back]))
            # Do NOT add the edge — keep the graph acyclic for later candidates.
            continue
        adjacency.setdefault(edge.from_ticket_id, []).append(edge.to_ticket_id)
        to_persist.append(edge)

    if cycles:
        return BatchValidationCycle(
            cycles=cycles,
            dropped_intra_batch=dropped_intra_batch,
            dropped_persisted=dropped_persisted,
        )
    return BatchValidationOk(
        to_persist=to_persist,
        dropped_intra_batch=dropped_intra_batch,
        dropped_persisted=dropped_persisted,
    )


def _format_cycle(cycle: DetectedCycle) -> str:
    """Render a cycle as an arrow chain of ticket ids (``t1 -> t2 -> t1``)."""

    path = cycle.cycle_path
    if not path:
        return ""
    nodes = [path[0].from_ticket_id, *(e.to_ticket_id for e in path)]
    return " -> ".join(nodes)


def validate_dependencies_batch(
    incoming: Sequence[SpecDependencyEdge],
    existing: Sequence[SpecDependencyEdge],
    tickets: Sequence[TicketRef] = (),
) -> PrecheckResult:
    """Pre-check wrapper over :func:`run_dependencies_batch` → :class:`Accepted` | :class:`Denied`.

    ``cycle_detected`` (with formatted cycle paths as ``blockers``) takes
    precedence; otherwise ``batch_fully_deduped`` when nothing survives dedup;
    otherwise accepted. The L4 pipeline calls :func:`run_dependencies_batch`
    directly when it also needs the surviving ``to_persist`` edges.
    """

    result = run_dependencies_batch(incoming, existing, tickets)

    if isinstance(result, BatchValidationCycle):
        return Denied(
            code="cycle_detected",
            message="Adding these dependencies would create a circular dependency.",
            context={"cycle_count": len(result.cycles)},
            blockers=[_format_cycle(c) for c in result.cycles],
        )

    if not result.to_persist:
        return Denied(
            code="batch_fully_deduped",
            message=(
                "Every submitted dependency is a duplicate (within the batch or "
                "already persisted); nothing new to add."
            ),
            context={
                "dropped_intra_batch": len(result.dropped_intra_batch),
                "dropped_persisted": len(result.dropped_persisted),
            },
        )

    return Accepted()
