"""Dependency-tree builder + structural-change gate (pure).

The DP/SCC/blocker-depth heart lives in :mod:`specsmither.dag.critical_path`; this
module is the *wiring*: it derives the deduped/sorted adjacency, the per-ticket
``unsatisfied_deps``/``blocker_count``/``is_ready`` fields, the rollup
``summary``, and the :func:`compute_tree_update` gate that lets the recompute
worklist skip a write (and a version bump) when nothing structural changed.

Outputs are pinned by golden fixtures, so every ordering and default is
deterministic:

* **Sorted everything** (rule 4): tickets/epics iterated in sorted id order;
  ``dependencies``/``blocks`` deduped + sorted; ``edges`` sorted by ``(from, to)``;
  ``ticket_ids``/``ready_tickets``/``blocked_tickets`` sorted by
  ``(ticket_number, id)``.
* **Critical-path defaults** (rule 2): selection weighs a missing/0 estimate as
  ``1`` but the rendered :class:`CriticalPathNode` and ``estimated_minutes_total``
  default a *missing* estimate to ``60`` (a literal ``0`` stays ``0``). Both are
  inherited from :func:`compute_critical_path`.
* **Half-up minutes** (rule 1): ``critical_path_minutes`` is the ``floor(x+0.5)``
  of the summed DP weights (also inherited).
* **Exclusions**: the critical path drops ``done`` + cycle nodes; blocker depth
  drops cycle nodes; ``has_circular_deps = len(cycles) > 0``.
* **Number/title guards**: a missing ``ticket_number``/``epic_number`` renders
  ``0`` and a missing/empty ``title`` renders :data:`UNNUMBERED_PLACEHOLDER`.

The reverse-edge ``TreeEdge.from`` is a Python keyword, so it is spelled
:attr:`TreeEdge.from_` (``to`` is unchanged); a JSON serializer for the golden
harness maps ``from_`` -> ``from``.

Determinism note on :func:`compute_tree_update`: structural equality IGNORES the
volatile ``generated_at`` and ``version`` and compares only ``edges``, ``cycles``,
each ticket's ``{status, is_ready, blocker_count, dependencies, blocks}`` and the
summary's ``{total_tickets, has_circular_deps, critical_path_length,
ready_tickets, blocked_tickets}``. When unchanged it returns the PREVIOUS tree
(``has_changed == False``) so the caller does NOT bump the version — preserving
the cost-saver gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from specsmither.dag.critical_path import (
    UNNUMBERED_PLACEHOLDER,
    CriticalPathNode,
    compute_critical_path,
    compute_max_blocker_depth,
    find_cycles,
)
from specsmither.dag.types import DepEdge, EpicNode, TicketNode
from specsmither.ids import now_iso

__all__ = [
    "UNNUMBERED_PLACEHOLDER",
    "CriticalPathNode",
    "DependencyTree",
    "ExtendedTreeSummary",
    "TicketsByStatus",
    "TreeCycle",
    "TreeEdge",
    "TreeEpic",
    "TreeSummary",
    "TreeTicket",
    "build_dependency_tree",
    "compute_tree_update",
]

_DONE = "done"
#: Statuses a ready ticket may carry to be surfaced in ``summary.readyTickets``.
_READY_BUCKET = ("pending", "ready")


@dataclass(frozen=True, slots=True)
class TreeEdge:
    """A directed dependency edge (``{from, to}``).

    ``from`` is a Python keyword, so the dependent end is :attr:`from_`; the
    blocker end is :attr:`to`. ``from_`` serializes back to ``from`` for the
    golden JSON.
    """

    from_: str
    to: str


@dataclass(frozen=True, slots=True)
class TreeCycle:
    """One detected cycle (``{cycle: [...]}``), ids sorted."""

    cycle: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TreeEpic:
    """An epic node in the tree (minus the input-only ``status``).

    The shared :class:`~specsmither.dag.types.EpicNode` carries no status (epic
    status is a counts/rollup concern derived in L4, not a pure-DAG output), so it
    is intentionally absent here. :attr:`ticket_ids` are sorted by
    ``(ticket_number, id)``.
    """

    id: str
    epic_number: int
    title: str
    ticket_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TreeTicket:
    """A ticket node in the tree.

    Derived fields: :attr:`unsatisfied_deps` are the dependencies whose status is
    not ``done``; :attr:`blocker_count` is their count; :attr:`is_ready` is
    ``blocker_count == 0 and status != 'done'``. :attr:`estimated_minutes` is the
    raw input estimate (may be ``None``/``0``); the ``60`` display default only
    applies to the critical path / total.
    """

    id: str
    ticket_number: int
    title: str
    status: str
    epic_id: str | None
    epic_number: int
    dependencies: tuple[str, ...]
    blocks: tuple[str, ...]
    is_ready: bool
    blocker_count: int
    unsatisfied_deps: tuple[str, ...]
    estimated_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class TicketsByStatus:
    """The ``summary.ticketsByStatus`` histogram (only these four buckets)."""

    pending: int
    ready: int
    active: int
    done: int


@dataclass(frozen=True, slots=True)
class TreeSummary:
    """Rollup summary.

    :attr:`critical_path_length` and :attr:`max_blocker_depth` are the scalars
    ``applyTree`` materializes onto the spec row (L4 reads them without parsing
    the tree JSON). :attr:`critical_path_minutes` is the half-up round of the
    summed DP weights.
    """

    total_tickets: int
    tickets_by_status: TicketsByStatus
    ready_tickets: tuple[str, ...]
    blocked_tickets: tuple[str, ...]
    has_circular_deps: bool
    critical_path_length: int
    critical_path_minutes: int
    max_blocker_depth: int


@dataclass(frozen=True, slots=True)
class ExtendedTreeSummary(TreeSummary):
    """:class:`TreeSummary` + the critical-path nodes and estimate total.

    :attr:`estimated_minutes_total` sums each ticket's estimate with the ``60``
    display default for missing estimates.
    """

    critical_path: tuple[CriticalPathNode, ...]
    estimated_minutes_total: int


@dataclass(frozen=True, slots=True)
class DependencyTree:
    """The full pre-computed dependency tree.

    :attr:`generated_at` and :attr:`version` are volatile and ignored by
    :func:`compute_tree_update`'s structural equality. The
    :attr:`critical_path_length` / :attr:`max_blocker_depth` properties surface
    the materialized scalars at the tree level for the L4 ``applyTree`` writer.
    """

    specification_id: str
    version: int
    generated_at: str
    epics: tuple[TreeEpic, ...]
    tickets: dict[str, TreeTicket]
    summary: ExtendedTreeSummary
    critical_path: tuple[CriticalPathNode, ...]
    edges: tuple[TreeEdge, ...]
    cycles: tuple[TreeCycle, ...]

    @property
    def critical_path_length(self) -> int:
        """Materialized scalar — the critical-path node count (spec-row column)."""
        return self.summary.critical_path_length

    @property
    def max_blocker_depth(self) -> int:
        """Materialized scalar — the longest incomplete blocking chain (spec row)."""
        return self.summary.max_blocker_depth


def build_dependency_tree(
    specification_id: str,
    epics: list[EpicNode],
    tickets: list[TicketNode],
    edges: list[DepEdge],
    *,
    previous_version: int | None = None,
    generated_at: str | None = None,
) -> DependencyTree:
    """Build a full dependency tree from raw entity snapshots (pure).

    ``generated_at`` is volatile: it defaults to
    :func:`specsmither.db.base.now_iso` but tests pin it. ``version`` auto-
    increments to ``(previous_version ?? 0) + 1`` on every build — the
    :func:`compute_tree_update` gate decides whether that bump is kept.
    """

    valid_ids = {t.id for t in tickets}
    tickets_sorted = sorted(tickets, key=lambda t: t.id)
    epics_sorted = sorted(epics, key=lambda e: e.id)

    # dependsOn map: deduped + sorted, seeded for every ticket id.
    depends_on: dict[str, list[str]] = {t.id: [] for t in tickets_sorted}
    for edge in edges:
        if edge.ticket_id in valid_ids and edge.depends_on_id in valid_ids:
            depends_on[edge.ticket_id].append(edge.depends_on_id)
    for tid in depends_on:
        depends_on[tid] = sorted(set(depends_on[tid]))

    # Canonical edges (sorted by from then to) reused by every DAG aggregator.
    canonical_edges: list[DepEdge] = [
        DepEdge(ticket_id=t.id, depends_on_id=dep_id)
        for t in tickets_sorted
        for dep_id in depends_on[t.id]
    ]
    canonical_edges.sort(key=lambda e: (e.ticket_id, e.depends_on_id))
    tree_edges = tuple(TreeEdge(from_=e.ticket_id, to=e.depends_on_id) for e in canonical_edges)

    # Reverse edges (blocks): deduped + sorted.
    blocks: dict[str, list[str]] = {t.id: [] for t in tickets_sorted}
    for t in tickets_sorted:
        for dep_id in depends_on[t.id]:
            blocks[dep_id].append(t.id)
    for tid in blocks:
        blocks[tid] = sorted(set(blocks[tid]))

    epic_number_by_id: dict[str | None, int | None] = {e.id: e.epic_number for e in epics_sorted}
    status_by_id = {t.id: t.status for t in tickets_sorted}

    # Ticket nodes (insertion order = sorted id order).
    tree_tickets: dict[str, TreeTicket] = {}
    for t in tickets_sorted:
        deps = tuple(depends_on[t.id])
        unsatisfied = tuple(d for d in deps if d in status_by_id and status_by_id[d] != _DONE)
        blocker_count = len(unsatisfied)
        epic_number = epic_number_by_id.get(t.epic_id)
        tree_tickets[t.id] = TreeTicket(
            id=t.id,
            ticket_number=t.ticket_number if t.ticket_number is not None else 0,
            title=t.title if t.title else UNNUMBERED_PLACEHOLDER,
            status=t.status,
            epic_id=t.epic_id,
            epic_number=epic_number if epic_number is not None else 0,
            dependencies=deps,
            blocks=tuple(blocks[t.id]),
            is_ready=blocker_count == 0 and t.status != _DONE,
            blocker_count=blocker_count,
            unsatisfied_deps=unsatisfied,
            estimated_minutes=t.estimated_minutes,
        )

    # Epic ticket-id lists, sorted by (ticketNumber, id).
    ticket_ids_by_epic: dict[str | None, list[str]] = {}
    for t in tickets_sorted:
        ticket_ids_by_epic.setdefault(t.epic_id, []).append(t.id)
    for epic_id in ticket_ids_by_epic:
        ticket_ids_by_epic[epic_id].sort(key=lambda i: (tree_tickets[i].ticket_number, i))

    tree_epics = tuple(
        TreeEpic(
            id=e.id,
            epic_number=e.epic_number if e.epic_number is not None else 0,
            title=e.title if e.title else UNNUMBERED_PLACEHOLDER,
            ticket_ids=tuple(ticket_ids_by_epic.get(e.id, ())),
        )
        for e in epics_sorted
    )

    # Cycles (Tarjan SCC) -> the exclusion set for path + blocker depth.
    raw_cycles = find_cycles([t.id for t in tickets_sorted], canonical_edges)
    cycles = tuple(TreeCycle(cycle=tuple(c)) for c in raw_cycles)
    cycle_nodes = frozenset(node for c in raw_cycles for node in c)

    # Critical path excludes done + cycle nodes; blocker depth excludes cycles.
    done_ids = frozenset(t.id for t in tickets_sorted if t.status == _DONE)
    cp = compute_critical_path(tickets_sorted, canonical_edges, exclude=cycle_nodes | done_ids)
    max_blocker_depth = compute_max_blocker_depth(
        tickets_sorted, canonical_edges, exclude=cycle_nodes
    )

    # Status histogram + ready/blocked lists.
    counts = {"pending": 0, "ready": 0, "active": 0, "done": 0}
    ready_list: list[str] = []
    blocked_list: list[str] = []
    for t in tickets_sorted:
        tk = tree_tickets[t.id]
        if tk.status in counts:
            counts[tk.status] += 1
        if tk.is_ready and tk.status in _READY_BUCKET:
            ready_list.append(tk.id)
        if tk.blocker_count > 0 and tk.status != _DONE:
            blocked_list.append(tk.id)
    ready_list.sort(key=lambda i: (tree_tickets[i].ticket_number, i))
    blocked_list.sort(key=lambda i: (tree_tickets[i].ticket_number, i))

    estimated_minutes_total = sum(
        (t.estimated_minutes if t.estimated_minutes is not None else 60) for t in tickets_sorted
    )

    summary = ExtendedTreeSummary(
        total_tickets=len(tickets_sorted),
        tickets_by_status=TicketsByStatus(
            pending=counts["pending"],
            ready=counts["ready"],
            active=counts["active"],
            done=counts["done"],
        ),
        ready_tickets=tuple(ready_list),
        blocked_tickets=tuple(blocked_list),
        has_circular_deps=len(raw_cycles) > 0,
        critical_path_length=cp.length,
        critical_path_minutes=cp.minutes,
        max_blocker_depth=max_blocker_depth,
        critical_path=cp.path,
        estimated_minutes_total=estimated_minutes_total,
    )

    return DependencyTree(
        specification_id=specification_id,
        version=(previous_version if previous_version is not None else 0) + 1,
        generated_at=generated_at if generated_at is not None else now_iso(),
        epics=tree_epics,
        tickets=tree_tickets,
        summary=summary,
        critical_path=cp.path,
        edges=tree_edges,
        cycles=cycles,
    )


def compute_tree_update(
    previous: DependencyTree | None,
    current: DependencyTree,
) -> tuple[bool, DependencyTree]:
    """The cost-saver gate: has the tree changed?

    Returns ``(has_changed, tree)``. With no ``previous`` tree the build is
    always a change ``(True, current)``. Otherwise structural equality is checked
    while IGNORING ``generated_at`` and ``version``; if unchanged the PREVIOUS
    tree is returned ``(False, previous)`` so the caller skips the write and the
    version bump. Only a genuine change yields ``(True, current)`` (whose
    ``version`` was already incremented by :func:`build_dependency_tree`).
    """

    if previous is None:
        return True, current
    if _tree_structurally_equal(previous, current):
        return False, previous
    return True, current


def _tree_structurally_equal(a: DependencyTree, b: DependencyTree) -> bool:
    """Structural equality (ignores ``generated_at`` + ``version``)."""

    if a.specification_id != b.specification_id:
        return False
    if a.edges != b.edges:
        return False
    if a.cycles != b.cycles:
        return False

    if sorted(a.tickets) != sorted(b.tickets):
        return False
    for tid in a.tickets:
        ta = a.tickets[tid]
        tb = b.tickets[tid]
        if (
            ta.status != tb.status
            or ta.is_ready != tb.is_ready
            or ta.blocker_count != tb.blocker_count
            or ta.dependencies != tb.dependencies
            or ta.blocks != tb.blocks
        ):
            return False

    sa = a.summary
    sb = b.summary
    return not (
        sa.total_tickets != sb.total_tickets
        or sa.has_circular_deps != sb.has_circular_deps
        or sa.critical_path_length != sb.critical_path_length
        or sa.ready_tickets != sb.ready_tickets
        or sa.blocked_tickets != sb.blocked_tickets
    )
