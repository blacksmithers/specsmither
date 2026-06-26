"""Pure count derivations — the rollup arithmetic, recompute-from-children.

Ports the PURE ``derive*`` / distinct / change-detection logic from the SpecForge
``packages/operations/src/aggregators/counts/*`` modules. The ``apply*`` /
atomic-ADD-delta machinery is DROPPED on purpose: SpecSmither is a single-writer
SQLite store that recomputes each parent's denormalized counts from its children
inside the mutation transaction (recon A5 §4 "SpecSmither recommendation"). There
are no DynamoDB stream deltas to coalesce, so every function here takes the FULL
child set and returns the absolute counts (frozen dataclasses whose fields are the
snake_case ORM count columns).

Determinism (recon A5 §4):
- ``progress`` EVERYWHERE = ``floor(completed / total * 100 + 0.5)`` (half-up),
  ``0`` when ``total == 0``. NOT Python ``round`` (banker's). :func:`progress_pct`.
- ``deriveEpicStatus`` -> ``todo | in_progress | completed`` (verbatim TS rule).
- ``inReviewSpecCount`` folds ``{ready_for_review, in_review, reviewed}`` into one
  bucket; the six spec-status buckets + ``completed`` (``done``) partition all 8
  ``SpecStatus`` values exactly (sum == ``spec_count``).
- dep counts: per edge -> ``outgoing`` on the ``ticket_id`` (from/dependent) end,
  ``incoming`` on the ``depends_on_id`` (to/blocker) end; spec total counts each
  intra-spec edge ONCE; epic total counts an edge ONLY when both endpoints share
  an epic.
- blueprint counts: spec/ticket are plain row counts; epic is the count of
  DISTINCT blueprint ids across the epic's tickets' refs (re-derived, never a
  per-edge delta) — :func:`count_distinct_blueprint_ids`.

Design note (recompute, not sum-of-deltas): :func:`derive_spec_counts` takes BOTH
the per-epic derived statuses (to bucket epics) AND the spec's own tickets (to
roll up ticket counts directly), so the spec's ticket aggregates come straight
from its grandchildren rather than from possibly-stale per-epic count columns.
:func:`derive_project_counts` rolls up from per-spec count *views* (a project has
no tickets of its own — its children are specs).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from specsmither.dag.types import DepEdge, TicketNode
from specsmither.domain.enums import EpicStatus, SpecStatus, TicketStatus

__all__ = [
    "BlueprintCountResult",
    "BlueprintRef",
    "DependencyCountResult",
    "EpicCounts",
    "EpicCountsView",
    "ProjectCounts",
    "SpecCounts",
    "SpecCountsView",
    "count_distinct_blueprint_ids",
    "derive_blueprint_counts",
    "derive_dependency_counts",
    "derive_epic_counts",
    "derive_epic_status",
    "derive_project_counts",
    "derive_spec_counts",
    "has_epic_counts_changed",
    "has_project_counts_changed",
    "has_spec_counts_changed",
    "progress_pct",
]


# --------------------------------------------------------------------------- #
# progress                                                                     #
# --------------------------------------------------------------------------- #


def progress_pct(completed: int, total: int) -> int:
    """Unified half-up progress percentage (recon A5 §4, Bug-9 formula).

    ``0`` when ``total == 0``; otherwise ``floor(completed / total * 100 + 0.5)``.
    Uses ``math.floor(x + 0.5)`` rather than Python ``round`` so half rounds toward
    +inf (matching the TS ``Math.round``): ``2/3 -> 67``, ``1/8 -> 13``.
    """
    if total == 0:
        return 0
    return math.floor(completed / total * 100 + 0.5)


# --------------------------------------------------------------------------- #
# ticket tally (shared by epic + spec rollups)                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _TicketTally:
    """Per-status ticket buckets + estimated-minutes sum over a set of tickets."""

    ticket_count: int
    pending_ticket_count: int
    ready_ticket_count: int
    active_ticket_count: int
    completed_ticket_count: int
    estimated_minutes: int


def _tally_tickets(tickets: Sequence[TicketNode]) -> _TicketTally:
    pending = ready = active = completed = 0
    estimated_minutes = 0
    for ticket in tickets:
        status = ticket.status
        if status == TicketStatus.PENDING:
            pending += 1
        elif status == TicketStatus.READY:
            ready += 1
        elif status == TicketStatus.ACTIVE:
            active += 1
        elif status == TicketStatus.DONE:
            completed += 1
        # Unknown statuses bucket nowhere (mirrors the TS switch with no default).
        estimated_minutes += ticket.estimated_minutes if ticket.estimated_minutes is not None else 0
    return _TicketTally(
        ticket_count=len(tickets),
        pending_ticket_count=pending,
        ready_ticket_count=ready,
        active_ticket_count=active,
        completed_ticket_count=completed,
        estimated_minutes=estimated_minutes,
    )


# --------------------------------------------------------------------------- #
# epic counts                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EpicCounts:
    """Derived Epic count columns (output of :func:`derive_epic_counts`).

    Fields map 1:1 to the ``Epic`` ORM count columns (``status`` is the derived
    ``EpicStatus``). The dependency/blueprint counts on the Epic row are produced
    separately by :func:`derive_dependency_counts` / :func:`derive_blueprint_counts`.
    """

    ticket_count: int
    pending_ticket_count: int
    ready_ticket_count: int
    active_ticket_count: int
    completed_ticket_count: int
    estimated_minutes: int
    progress: int
    status: EpicStatus


@dataclass(frozen=True, slots=True)
class EpicCountsView:
    """Minimal per-epic input for the spec rollup: just the derived status.

    The spec rolls its ticket aggregates up from its own tickets (see
    :func:`derive_spec_counts`), so all an epic needs to contribute to the spec is
    its derived ``EpicStatus`` (for the todo/in_progress/completed epic buckets).
    """

    status: EpicStatus

    @classmethod
    def from_counts(cls, counts: EpicCounts) -> EpicCountsView:
        """Project a freshly-derived :class:`EpicCounts` to the spec-rollup view."""
        return cls(status=counts.status)


def derive_epic_status(
    *,
    ticket_count: int,
    completed_ticket_count: int,
    active_ticket_count: int,
    progress: int,
) -> EpicStatus:
    """``deriveEpicStatus`` (derive-epic-counts.ts) -> ``todo|in_progress|completed``.

    Rule (order matters): no tickets -> ``todo``; ``progress == 100`` -> ``completed``;
    any active or completed ticket -> ``in_progress``; else ``todo`` (only pending
    and/or ready tickets present).
    """
    if ticket_count == 0:
        return EpicStatus.TODO
    if progress == 100:
        return EpicStatus.COMPLETED
    if active_ticket_count > 0 or completed_ticket_count > 0:
        return EpicStatus.IN_PROGRESS
    return EpicStatus.TODO


def derive_epic_counts(epic_tickets: Sequence[TicketNode]) -> EpicCounts:
    """Recompute an Epic's counts from its full ticket set."""
    tally = _tally_tickets(epic_tickets)
    progress = progress_pct(tally.completed_ticket_count, tally.ticket_count)
    status = derive_epic_status(
        ticket_count=tally.ticket_count,
        completed_ticket_count=tally.completed_ticket_count,
        active_ticket_count=tally.active_ticket_count,
        progress=progress,
    )
    return EpicCounts(
        ticket_count=tally.ticket_count,
        pending_ticket_count=tally.pending_ticket_count,
        ready_ticket_count=tally.ready_ticket_count,
        active_ticket_count=tally.active_ticket_count,
        completed_ticket_count=tally.completed_ticket_count,
        estimated_minutes=tally.estimated_minutes,
        progress=progress,
        status=status,
    )


# --------------------------------------------------------------------------- #
# specification counts                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SpecCounts:
    """Derived Specification count columns (output of :func:`derive_spec_counts`).

    Fields map 1:1 to the scalar ``Specification`` count columns. The
    dependency/blueprint counts and the dependency-tree scalars
    (``critical_path_length``, ``max_blocker_depth``, ``dependency_tree*``) are
    produced by their own rollups, not here.
    """

    epic_count: int
    todo_epic_count: int
    in_progress_epic_count: int
    completed_epic_count: int
    ticket_count: int
    completed_ticket_count: int
    pending_ticket_count: int
    ready_ticket_count: int
    active_ticket_count: int
    estimated_minutes: int
    progress: int


@dataclass(frozen=True, slots=True)
class SpecCountsView:
    """Per-spec input for the project rollup: status + the rolled-up child counts.

    A project has no tickets of its own; it aggregates from its specs' count
    columns and buckets specs by lifecycle status. Mirrors the TS
    ``ProjectSpecificationSnapshot``.
    """

    status: SpecStatus
    epic_count: int
    completed_epic_count: int
    ticket_count: int
    completed_ticket_count: int

    @classmethod
    def from_counts(cls, status: SpecStatus, counts: SpecCounts) -> SpecCountsView:
        """Project a :class:`SpecCounts` (+ the spec's lifecycle status) to the view."""
        return cls(
            status=status,
            epic_count=counts.epic_count,
            completed_epic_count=counts.completed_epic_count,
            ticket_count=counts.ticket_count,
            completed_ticket_count=counts.completed_ticket_count,
        )


def derive_spec_counts(
    spec_epics: Sequence[EpicCountsView],
    spec_tickets: Sequence[TicketNode],
) -> SpecCounts:
    """Recompute a Specification's counts.

    Epic buckets (``todo``/``in_progress``/``completed``) come from the per-epic
    derived statuses in ``spec_epics``; ``epic_count == len(spec_epics)``. Ticket
    aggregates, ``estimated_minutes`` and ``progress`` are rolled up directly from
    ``spec_tickets`` (the spec's full ticket set), so they never depend on
    possibly-stale per-epic count columns.
    """
    todo = in_progress = completed = 0
    for epic in spec_epics:
        if epic.status == EpicStatus.TODO:
            todo += 1
        elif epic.status == EpicStatus.IN_PROGRESS:
            in_progress += 1
        elif epic.status == EpicStatus.COMPLETED:
            completed += 1

    tally = _tally_tickets(spec_tickets)
    progress = progress_pct(tally.completed_ticket_count, tally.ticket_count)
    return SpecCounts(
        epic_count=len(spec_epics),
        todo_epic_count=todo,
        in_progress_epic_count=in_progress,
        completed_epic_count=completed,
        ticket_count=tally.ticket_count,
        completed_ticket_count=tally.completed_ticket_count,
        pending_ticket_count=tally.pending_ticket_count,
        ready_ticket_count=tally.ready_ticket_count,
        active_ticket_count=tally.active_ticket_count,
        estimated_minutes=tally.estimated_minutes,
        progress=progress,
    )


# --------------------------------------------------------------------------- #
# project counts                                                              #
# --------------------------------------------------------------------------- #

#: Spec lifecycle statuses that fold into the single ``in_review`` project bucket.
_REVIEW_SPEC_STATUSES: frozenset[SpecStatus] = frozenset(
    {SpecStatus.READY_FOR_REVIEW, SpecStatus.IN_REVIEW, SpecStatus.REVIEWED}
)


@dataclass(frozen=True, slots=True)
class ProjectCounts:
    """Derived Project count columns (output of :func:`derive_project_counts`).

    NO ``progress`` — the ``Project`` ORM row has no progress column (the read side
    derives it from ``completed_ticket_count / ticket_count``). The six per-status
    spec buckets + ``completed_spec_count`` partition all 8 ``SpecStatus`` values
    exactly, so their sum equals ``spec_count``.
    """

    spec_count: int
    completed_spec_count: int
    draft_spec_count: int
    planning_spec_count: int
    ready_spec_count: int
    in_progress_spec_count: int
    in_review_spec_count: int
    epic_count: int
    completed_epic_count: int
    ticket_count: int
    completed_ticket_count: int


def derive_project_counts(project_specs: Sequence[SpecCountsView]) -> ProjectCounts:
    """Recompute a Project's counts from its full set of per-spec count views."""
    draft = planning = ready = in_progress = in_review = completed_specs = 0
    epic_count = completed_epic_count = ticket_count = completed_ticket_count = 0
    for spec in project_specs:
        status = spec.status
        if status == SpecStatus.DRAFT:
            draft += 1
        elif status == SpecStatus.PLANNING:
            planning += 1
        elif status == SpecStatus.READY:
            ready += 1
        elif status == SpecStatus.IN_PROGRESS:
            in_progress += 1
        elif status in _REVIEW_SPEC_STATUSES:
            in_review += 1
        elif status == SpecStatus.DONE:
            completed_specs += 1
        epic_count += spec.epic_count
        completed_epic_count += spec.completed_epic_count
        ticket_count += spec.ticket_count
        completed_ticket_count += spec.completed_ticket_count

    return ProjectCounts(
        spec_count=len(project_specs),
        completed_spec_count=completed_specs,
        draft_spec_count=draft,
        planning_spec_count=planning,
        ready_spec_count=ready,
        in_progress_spec_count=in_progress,
        in_review_spec_count=in_review,
        epic_count=epic_count,
        completed_epic_count=completed_epic_count,
        ticket_count=ticket_count,
        completed_ticket_count=completed_ticket_count,
    )


# --------------------------------------------------------------------------- #
# dependency counts                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DependencyCountResult:
    """Derived dependency counts for one spec's intra-spec edge set.

    - ``ticket_incoming`` -> ``Ticket.incoming_dep_count`` (the ``depends_on_id``
      / blocker end of each edge).
    - ``ticket_outgoing`` -> ``Ticket.outgoing_dep_count`` (the ``ticket_id`` /
      dependent end of each edge).
    - ``specification`` -> ``Specification.dependency_count`` (every intra-spec
      edge counted once == ``len(edges)``).
    - ``epic`` -> per-epic ``Epic.dependency_count`` (only edges whose BOTH
      endpoints belong to the same epic).
    """

    ticket_incoming: Mapping[str, int]
    ticket_outgoing: Mapping[str, int]
    specification: int
    epic: Mapping[str, int]


def _bump(counter: dict[str, int], key: str | None, delta: int = 1) -> None:
    if key is None:
        return
    counter[key] = counter.get(key, 0) + delta


def derive_dependency_counts(
    edges: Sequence[DepEdge],
    ticket_epic: Mapping[str, str | None],
) -> DependencyCountResult:
    """Recompute dependency counts from a spec's full intra-spec edge set.

    ``edges`` are the ``TicketDependency`` rows of a single spec (dependencies are
    intra-spec by construction). ``ticket_epic`` maps each ticket id to its epic id
    (``None`` if unknown/unassigned) and is used solely to decide whether an edge
    is intra-epic.
    """
    incoming: dict[str, int] = {}
    outgoing: dict[str, int] = {}
    epic: dict[str, int] = {}
    for edge in edges:
        _bump(outgoing, edge.ticket_id)  # dependent (from) end
        _bump(incoming, edge.depends_on_id)  # blocker (to) end
        from_epic = ticket_epic.get(edge.ticket_id)
        to_epic = ticket_epic.get(edge.depends_on_id)
        if from_epic is not None and from_epic == to_epic:
            _bump(epic, from_epic)
    return DependencyCountResult(
        ticket_incoming=incoming,
        ticket_outgoing=outgoing,
        specification=len(edges),
        epic=epic,
    )


# --------------------------------------------------------------------------- #
# blueprint counts                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BlueprintRef:
    """A ticket↔blueprint reference row (the pure projection of ``TicketBlueprintRef``)."""

    ticket_id: str
    blueprint_id: str


@dataclass(frozen=True, slots=True)
class BlueprintCountResult:
    """Derived blueprint counts.

    - ``specification`` -> ``Specification.blueprint_count`` (# blueprint rows in
      the spec — a plain row count).
    - ``ticket`` -> per-ticket ``Ticket.blueprint_count`` (# reference rows; the
      ``(ticket_id, blueprint_id)`` UNIQUE constraint makes this == distinct).
    - ``epic`` -> per-epic ``Epic.blueprint_count`` (count of DISTINCT blueprint
      ids across the epic's tickets' refs — a blueprint linked by two tickets in
      the same epic counts ONCE).
    """

    specification: int
    ticket: Mapping[str, int]
    epic: Mapping[str, int]


def count_distinct_blueprint_ids(refs_for_epic: Iterable[str]) -> int:
    """``countDistinctBlueprintIds`` — distinct blueprint cardinality for one epic.

    ``refs_for_epic`` is the flat collection of blueprint ids referenced by every
    ticket in the epic; duplicates (the same blueprint linked by multiple tickets)
    collapse to one.
    """
    return len(set(refs_for_epic))


def derive_blueprint_counts(
    spec_blueprint_ids: Sequence[str],
    ticket_refs: Sequence[BlueprintRef],
    ticket_epic: Mapping[str, str | None],
) -> BlueprintCountResult:
    """Recompute blueprint counts for one spec.

    ``spec_blueprint_ids`` is one entry per ``Blueprint`` row owned by the spec
    (its length is the spec blueprint count). ``ticket_refs`` are the
    ``TicketBlueprintRef`` rows across the spec's tickets. ``ticket_epic`` maps
    each ticket id to its epic id (``None`` if unknown) to group refs per epic for
    the distinct-count.
    """
    ticket_counts: dict[str, int] = {}
    epic_blueprint_ids: dict[str, set[str]] = {}
    for ref in ticket_refs:
        _bump(ticket_counts, ref.ticket_id)
        epic_id = ticket_epic.get(ref.ticket_id)
        if epic_id is not None:
            epic_blueprint_ids.setdefault(epic_id, set()).add(ref.blueprint_id)
    epic_counts = {
        epic_id: count_distinct_blueprint_ids(ids) for epic_id, ids in epic_blueprint_ids.items()
    }
    return BlueprintCountResult(
        specification=len(spec_blueprint_ids),
        ticket=ticket_counts,
        epic=epic_counts,
    )


# --------------------------------------------------------------------------- #
# change-detection gates (write-skip optimization)                            #
# --------------------------------------------------------------------------- #
# The recomputed dataclasses contain ONLY the count columns, so the generated
# frozen-dataclass ``__eq__`` is exactly the field-wise compare the TS
# ``has*CountsChanged`` helpers do by hand. These thin wrappers let the recompute
# worklist (#14) skip a no-op write; recompute-from-children remains the truth.


def has_epic_counts_changed(current: EpicCounts, derived: EpicCounts) -> bool:
    """True iff any derived Epic count column differs from the stored one."""
    return current != derived


def has_spec_counts_changed(current: SpecCounts, derived: SpecCounts) -> bool:
    """True iff any derived Specification count column differs from the stored one."""
    return current != derived


def has_project_counts_changed(current: ProjectCounts, derived: ProjectCounts) -> bool:
    """True iff any derived Project count column differs from the stored one."""
    return current != derived
