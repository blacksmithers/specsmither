"""In-transaction recompute worklist — the heart of architecture decision 2.

This module materializes *all* derived data for an affected specification
SYNCHRONOUSLY, inside the caller's mutation ``Session.begin()``. It is the bridge
between the ORM (L2 authoritative rows) and the pure DAG/counts engine (L3 + the
:mod:`specsmither.rollups.counts` derivations): given a live :class:`Session` and
the set of specs a mutation touched, it rewrites ticket statuses (cascade),
the epic/spec/project denormalized count columns, the per-edge dependency counts,
the blueprint counts, and each spec's cached ``dependency_tree`` +
``critical_path_length`` + ``max_blocker_depth``.

Why recompute-from-children (not sum-of-deltas): SpecSmither is a single-writer
SQLite store, so instead of coalescing DynamoDB-stream deltas we re-derive every
parent's counts from its authoritative child rows in the same transaction. A spec
is a bounded ``O(V+E)`` neighborhood (sub-millisecond), so recomputing the FULL
affected spec is a correct superset of the precise per-mutation trigger switch —
no delta bookkeeping, no races, self-healing.

Cascade-to-fixpoint (batch re-derive): we load ``states = {ticket_id: status}``
and, for every ``should_recalculate`` ticket, re-derive its status from its
dependencies via :func:`calculate_ticket_status`, applying changes in place and
repeating until a full pass makes no change. This terminates because the cascade
only ever produces ``pending``/``ready`` (never ``done``), so the set of ``done``
tickets — the only thing a status decision depends on — is fixed for the duration
of one recompute; a ``ready`` ticket can never re-trigger a completion. On a
``ready -> pending`` demote we stamp a short English ``block_reason``; on a
``pending -> ready`` promote we clear it (``None``).

The caller owns the transaction: :func:`recompute` NEVER commits. It mutates ORM
instances (marking them dirty) and lets the enclosing ``Session.begin()`` flush
and commit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from specsmither.dag.cascade import find_dependencies
from specsmither.dag.status_calculator import calculate_ticket_status, should_recalculate
from specsmither.dag.tree import (
    CriticalPathNode,
    DependencyTree,
    ExtendedTreeSummary,
    TicketsByStatus,
    TreeCycle,
    TreeEdge,
    TreeEpic,
    TreeTicket,
    build_dependency_tree,
    compute_tree_update,
)
from specsmither.dag.types import DepEdge, EpicNode, TicketNode
from specsmither.db.base import now_iso
from specsmither.db.models import (
    Blueprint,
    Epic,
    Project,
    Specification,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
)
from specsmither.domain.enums import SpecStatus, TicketStatus
from specsmither.rollups.counts import (
    BlueprintRef,
    EpicCountsView,
    SpecCountsView,
    derive_blueprint_counts,
    derive_dependency_counts,
    derive_epic_counts,
    derive_project_counts,
    derive_spec_counts,
)

__all__ = ["recompute"]

_DONE = TicketStatus.DONE.value
_READY = TicketStatus.READY.value
_PENDING = TicketStatus.PENDING.value
#: Sentinel for an unknown/missing dependency status (always non-``done`` so it
#: blocks promotion). The full-neighborhood load means this is never hit in
#: practice; it is a parity guard mirroring the pure cascade engine.
_UNKNOWN = ""


def recompute(
    session: Session,
    *,
    spec_ids: Iterable[str],
    changed_ticket_ids: Iterable[str] | None = None,
    generated_at: str | None = None,
) -> None:
    """Materialize all derived data for ``spec_ids`` inside the caller's txn.

    For each affected spec id, re-cascades ticket statuses to fixpoint and
    rewrites the epic/spec count columns, the dependency/blueprint counts and the
    cached dependency tree (only bumping ``dependency_tree_version`` when the tree
    structurally changed). Each affected project's rollup is then recomputed once.

    Parameters
    ----------
    session:
        The live session of the *enclosing* mutation transaction. This function
        does NOT commit — it mutates ORM rows and defers to ``Session.begin()``.
    spec_ids:
        The specs the mutation touched. Recomputing the full spec is a correct
        superset of the precise per-mutation trigger; duplicates are de-duped and
        a missing spec id is skipped.
    changed_ticket_ids:
        Optional hint (the precise tickets a mutation changed). The batch
        re-derive is self-healing and does not need it, so it is accepted for the
        caller contract but not required for correctness.
    generated_at:
        ISO timestamp stamped on a rewritten tree (``dependency_tree_updated_at``)
        and threaded into the tree build. Defaults to :func:`now_iso` once, so
        every spec in one call shares the same instant.
    """
    del changed_ticket_ids  # accepted as a caller hint; batch re-derive is self-healing.
    gen_at = generated_at if generated_at is not None else now_iso()

    seen: set[str] = set()
    project_ids: set[str] = set()
    for spec_id in spec_ids:
        if spec_id in seen:
            continue
        seen.add(spec_id)
        project_id = _recompute_spec(session, spec_id, gen_at)
        if project_id is not None:
            project_ids.add(project_id)

    for project_id in project_ids:
        _recompute_project(session, project_id)


def _recompute_spec(session: Session, spec_id: str, gen_at: str) -> str | None:
    """Recompute one spec's full neighborhood. Returns its project id (or ``None``)."""
    spec = session.get(Specification, spec_id)
    if spec is None:
        return None

    epics = list(session.execute(select(Epic).where(Epic.specification_id == spec_id)).scalars())
    epic_ids = [e.id for e in epics]
    tickets = (
        list(session.execute(select(Ticket).where(Ticket.epic_id.in_(epic_ids))).scalars())
        if epic_ids
        else []
    )
    ticket_ids = {t.id for t in tickets}
    dep_rows = (
        list(
            session.execute(
                select(TicketDependency).where(TicketDependency.ticket_id.in_(ticket_ids))
            ).scalars()
        )
        if ticket_ids
        else []
    )
    # Dependencies are intra-spec by construction; keep only edges whose blocker
    # end is also in this spec (defensive).
    dep_edges = [
        DepEdge(ticket_id=d.ticket_id, depends_on_id=d.depends_on_id, type=d.type)
        for d in dep_rows
        if d.depends_on_id in ticket_ids
    ]
    blueprint_ids = list(
        session.execute(select(Blueprint.id).where(Blueprint.specification_id == spec_id)).scalars()
    )
    refs = (
        list(
            session.execute(
                select(TicketBlueprintRef).where(TicketBlueprintRef.ticket_id.in_(ticket_ids))
            ).scalars()
        )
        if ticket_ids
        else []
    )

    epic_number_by_id: dict[str, int] = {e.id: e.epic_number for e in epics}
    title_by_id: dict[str, str] = {t.id: t.title for t in tickets}
    ticket_epic: dict[str, str | None] = {t.id: t.epic_id for t in tickets}

    # 1. STATUS CASCADE to fixpoint (batch re-derive from the fixed done-set).
    states = _cascade_to_fixpoint(tickets, dep_edges)

    # 2. Apply status onto the authoritative rows, and re-derive the
    #    status-owned scalars (progress + the dep-derived block_reason) for
    #    EVERY ticket each pass — not only on a transition — so they never go
    #    stale when a pending ticket's dependency set changes without its
    #    status changing. progress is recompute-owned (a leaf ticket is 100
    #    when done, else 0); the stores never write it.
    for ticket in tickets:
        final = states[ticket.id]
        ticket.status = final
        ticket.progress = 100 if final == _DONE else 0
        if final == _PENDING:
            blockers = [d for d in find_dependencies(dep_edges, ticket.id) if states.get(d) != _DONE]
            ticket.block_reason = _format_block_reason(blockers, title_by_id) if blockers else None
        else:
            ticket.block_reason = None

    # Updated DAG views reflecting the post-cascade statuses.
    updated_nodes = [
        TicketNode(
            id=t.id,
            status=states[t.id],
            estimated_minutes=t.estimated_minutes,
            epic_id=t.epic_id,
            epic_number=epic_number_by_id.get(t.epic_id),
            ticket_number=t.ticket_number,
            title=t.title,
        )
        for t in tickets
    ]
    epic_nodes = [
        EpicNode(id=e.id, epic_number=e.epic_number, title=e.title, order=e.order) for e in epics
    ]

    # 3. COUNTS — epics, then the spec rollup.
    nodes_by_epic: dict[str, list[TicketNode]] = {e.id: [] for e in epics}
    for node in updated_nodes:
        if node.epic_id in nodes_by_epic:
            nodes_by_epic[node.epic_id].append(node)

    epic_views: list[EpicCountsView] = []
    for epic in epics:
        ec = derive_epic_counts(nodes_by_epic.get(epic.id, []))
        epic.ticket_count = ec.ticket_count
        epic.completed_ticket_count = ec.completed_ticket_count
        epic.pending_ticket_count = ec.pending_ticket_count
        epic.ready_ticket_count = ec.ready_ticket_count
        epic.active_ticket_count = ec.active_ticket_count
        epic.estimated_minutes = ec.estimated_minutes
        epic.progress = ec.progress
        epic.status = ec.status.value
        epic_views.append(EpicCountsView.from_counts(ec))

    sc = derive_spec_counts(epic_views, updated_nodes)
    spec.epic_count = sc.epic_count
    spec.todo_epic_count = sc.todo_epic_count
    spec.in_progress_epic_count = sc.in_progress_epic_count
    spec.completed_epic_count = sc.completed_epic_count
    spec.ticket_count = sc.ticket_count
    spec.completed_ticket_count = sc.completed_ticket_count
    spec.pending_ticket_count = sc.pending_ticket_count
    spec.ready_ticket_count = sc.ready_ticket_count
    spec.active_ticket_count = sc.active_ticket_count
    spec.estimated_minutes = sc.estimated_minutes
    spec.progress = sc.progress

    # 4. DEPENDENCY counts (per-edge: ticket in/out, spec-once, intra-epic only).
    dep_counts = derive_dependency_counts(dep_edges, ticket_epic)
    for ticket in tickets:
        ticket.incoming_dep_count = dep_counts.ticket_incoming.get(ticket.id, 0)
        ticket.outgoing_dep_count = dep_counts.ticket_outgoing.get(ticket.id, 0)
    spec.dependency_count = dep_counts.specification
    for epic in epics:
        epic.dependency_count = dep_counts.epic.get(epic.id, 0)

    # 5. BLUEPRINT counts (spec/ticket plain rows, epic distinct).
    bp_refs = [BlueprintRef(ticket_id=r.ticket_id, blueprint_id=r.blueprint_id) for r in refs]
    bp_counts = derive_blueprint_counts(blueprint_ids, bp_refs, ticket_epic)
    spec.blueprint_count = bp_counts.specification
    for ticket in tickets:
        ticket.blueprint_count = bp_counts.ticket.get(ticket.id, 0)
    for epic in epics:
        epic.blueprint_count = bp_counts.epic.get(epic.id, 0)

    # 6. TREE — rebuild, gate on structural change, materialize scalars on a write.
    previous_tree = (
        _tree_from_dict(spec.dependency_tree) if spec.dependency_tree is not None else None
    )
    current_tree = build_dependency_tree(
        spec_id,
        epic_nodes,
        updated_nodes,
        dep_edges,
        previous_version=spec.dependency_tree_version,
        generated_at=gen_at,
    )
    changed, tree = compute_tree_update(previous_tree, current_tree)
    if changed:
        spec.dependency_tree = asdict(tree)
        spec.dependency_tree_version = tree.version
        spec.dependency_tree_updated_at = gen_at
        spec.critical_path_length = tree.critical_path_length
        spec.max_blocker_depth = tree.max_blocker_depth

    return spec.project_id


def _recompute_project(session: Session, project_id: str) -> None:
    """Recompute a project's spec-bucket + child rollups from its spec rows."""
    project = session.get(Project, project_id)
    if project is None:
        return
    specs = list(
        session.execute(
            select(Specification).where(Specification.project_id == project_id)
        ).scalars()
    )
    views = [
        SpecCountsView(
            status=SpecStatus(s.status),
            epic_count=s.epic_count,
            completed_epic_count=s.completed_epic_count,
            ticket_count=s.ticket_count,
            completed_ticket_count=s.completed_ticket_count,
        )
        for s in specs
    ]
    pc = derive_project_counts(views)
    project.spec_count = pc.spec_count
    project.completed_spec_count = pc.completed_spec_count
    project.draft_spec_count = pc.draft_spec_count
    project.planning_spec_count = pc.planning_spec_count
    project.ready_spec_count = pc.ready_spec_count
    project.in_progress_spec_count = pc.in_progress_spec_count
    project.in_review_spec_count = pc.in_review_spec_count
    project.epic_count = pc.epic_count
    project.completed_epic_count = pc.completed_epic_count
    project.ticket_count = pc.ticket_count
    project.completed_ticket_count = pc.completed_ticket_count


# --------------------------------------------------------------------------- #
# cascade                                                                      #
# --------------------------------------------------------------------------- #


def _cascade_to_fixpoint(tickets: list[Ticket], edges: list[DepEdge]) -> dict[str, str]:
    """Re-derive recalculable ticket statuses to fixpoint (in-place batch pass).

    Terminates because the ``done`` set is fixed for the duration (the cascade
    only emits ``pending``/``ready``), so a second no-change pass always follows.
    """
    ids = [t.id for t in tickets]
    states: dict[str, str] = {t.id: t.status for t in tickets}
    changed = True
    while changed:
        changed = False
        for tid in ids:
            current = states[tid]
            if not should_recalculate(current):
                continue
            dep_statuses = [states.get(d, _UNKNOWN) for d in find_dependencies(edges, tid)]
            new_status = calculate_ticket_status(current, dep_statuses)
            if new_status != current:
                states[tid] = new_status
                changed = True
    return states


def _format_block_reason(blocker_ids: list[str], title_by_id: Mapping[str, str]) -> str:
    """Short English block reason for a ``ready -> pending`` demote."""
    count = len(blocker_ids)
    noun = "dependency" if count == 1 else "dependencies"
    names = ", ".join(title_by_id.get(b) or b for b in blocker_ids)
    return f"Blocked by {count} {noun}: {names}"


# --------------------------------------------------------------------------- #
# tree (de)serialization                                                       #
# --------------------------------------------------------------------------- #
# The stored ``dependency_tree`` JSON is ``dataclasses.asdict(tree)`` round-tripped
# through the JSON codec (tuples become lists). To feed ``compute_tree_update`` the
# previous tree, we rebuild a full :class:`DependencyTree` from that dict, restoring
# tuples so structural equality compares like-for-like against the fresh build.


def _tree_from_dict(data: dict[str, Any]) -> DependencyTree:
    return DependencyTree(
        specification_id=data["specification_id"],
        version=data["version"],
        generated_at=data["generated_at"],
        epics=tuple(
            TreeEpic(
                id=e["id"],
                epic_number=e["epic_number"],
                title=e["title"],
                ticket_ids=tuple(e["ticket_ids"]),
            )
            for e in data["epics"]
        ),
        tickets={tid: _ticket_from_dict(t) for tid, t in data["tickets"].items()},
        summary=_summary_from_dict(data["summary"]),
        critical_path=tuple(_cpnode_from_dict(n) for n in data["critical_path"]),
        edges=tuple(TreeEdge(from_=e["from_"], to=e["to"]) for e in data["edges"]),
        cycles=tuple(TreeCycle(cycle=tuple(c["cycle"])) for c in data["cycles"]),
    )


def _ticket_from_dict(t: dict[str, Any]) -> TreeTicket:
    return TreeTicket(
        id=t["id"],
        ticket_number=t["ticket_number"],
        title=t["title"],
        status=t["status"],
        epic_id=t["epic_id"],
        epic_number=t["epic_number"],
        dependencies=tuple(t["dependencies"]),
        blocks=tuple(t["blocks"]),
        is_ready=t["is_ready"],
        blocker_count=t["blocker_count"],
        unsatisfied_deps=tuple(t["unsatisfied_deps"]),
        estimated_minutes=t.get("estimated_minutes"),
    )


def _summary_from_dict(s: dict[str, Any]) -> ExtendedTreeSummary:
    tbs = s["tickets_by_status"]
    return ExtendedTreeSummary(
        total_tickets=s["total_tickets"],
        tickets_by_status=TicketsByStatus(
            pending=tbs["pending"],
            ready=tbs["ready"],
            active=tbs["active"],
            done=tbs["done"],
        ),
        ready_tickets=tuple(s["ready_tickets"]),
        blocked_tickets=tuple(s["blocked_tickets"]),
        has_circular_deps=s["has_circular_deps"],
        critical_path_length=s["critical_path_length"],
        critical_path_minutes=s["critical_path_minutes"],
        max_blocker_depth=s["max_blocker_depth"],
        critical_path=tuple(_cpnode_from_dict(n) for n in s["critical_path"]),
        estimated_minutes_total=s["estimated_minutes_total"],
    )


def _cpnode_from_dict(n: dict[str, Any]) -> CriticalPathNode:
    return CriticalPathNode(
        id=n["id"],
        ticket_number=n["ticket_number"],
        title=n["title"],
        estimated_minutes=n["estimated_minutes"],
    )
