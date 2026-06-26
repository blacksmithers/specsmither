"""Hypothesis property tests for the M0 DAG / recompute core.

Three properties the ``00-foundation.md`` acceptance requires:

1. **Cascade never promotes a ticket with an unsatisfied dependency.** Over a
   random *acyclic* ticket DAG (edges only ``i -> j`` with ``j < i``) plus a random
   initial status per ticket, drive the status fixpoint exactly as
   :mod:`specsmither.rollups.recompute` does (``should_recalculate`` +
   :func:`calculate_ticket_status` to fixpoint). INVARIANT: every ``ready`` ticket
   has ALL dependencies ``done``; ``active``/``done`` are sticky (never demoted) and
   are never produced by promotion.

2. **The dependency tree is acyclic except for the reported cycles.** Over a random
   graph that MAY contain cycles and self-loops, :func:`build_dependency_tree`
   reports a set ``C`` of cyclic nodes. INVARIANT: ``has_circular_deps == (len(cycles)
   > 0)``; the set of nodes that can reach themselves equals ``C`` exactly (so every
   self-loop and every SCC of size >= 2 is captured); the tree's edge set restricted
   to the nodes NOT in ``C`` is acyclic; the critical path is a simple path drawn
   only from non-cycle nodes.

3. **Denormalized counts == ``SELECT COUNT`` ground truth.** Build a random spec
   tree in a REAL SQLite database (``init_db``), run :func:`recompute`, then assert
   every denormalized count column equals a direct SQL ``COUNT`` over the child rows
   (and ``progress == floor(completed / total * 100 + 0.5)``).

The pure properties run many examples; the DB property is bounded and uses a fresh
temp database per example (so no function-scoped-fixture reuse). ``derandomize`` keeps
every run reproducible.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from specsmither.dag.cascade import find_dependencies
from specsmither.dag.status_calculator import calculate_ticket_status, should_recalculate
from specsmither.dag.tree import build_dependency_tree
from specsmither.dag.types import DepEdge, EpicNode, TicketNode
from specsmither.db.base import make_session_factory, new_ulid
from specsmither.db.migrations import init_db
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
from specsmither.rollups.recompute import recompute

_STATUSES = ("pending", "ready", "active", "done")
_STICKY = ("active", "done")


# --------------------------------------------------------------------------- #
# Property 1 — cascade never promotes a ticket with an unsatisfied dependency  #
# --------------------------------------------------------------------------- #


@st.composite
def _acyclic_graph(draw: st.DrawFn) -> tuple[list[str], dict[str, str], list[DepEdge]]:
    """A random ACYCLIC ticket DAG: ids, an initial status per id, edges (i -> j, j < i)."""
    n = draw(st.integers(min_value=1, max_value=8))
    ids = [f"N{i:03d}" for i in range(n)]
    initial = {tid: draw(st.sampled_from(_STATUSES)) for tid in ids}

    edges: list[DepEdge] = []
    seen: set[tuple[str, str]] = set()
    if n >= 2:
        m = draw(st.integers(min_value=0, max_value=2 * n))
        for _ in range(m):
            i = draw(st.integers(min_value=1, max_value=n - 1))
            j = draw(st.integers(min_value=0, max_value=i - 1))
            edge = (ids[i], ids[j])
            if edge in seen:
                continue
            seen.add(edge)
            edges.append(DepEdge(ticket_id=edge[0], depends_on_id=edge[1]))
    return ids, initial, edges


def _run_status_fixpoint(initial: dict[str, str], edges: list[DepEdge]) -> dict[str, str]:
    """Re-derive recalculable statuses to fixpoint, mirroring ``rollups.recompute``."""
    states = dict(initial)
    ids = list(states)
    # An acyclic DAG converges in <= depth passes; the bound also fails loudly if a
    # (buggy) non-terminating oscillation ever appeared.
    for _ in range(2 * len(ids) + 2):
        changed = False
        for tid in ids:
            current = states[tid]
            if not should_recalculate(current):
                continue
            dep_statuses = [states.get(d, "") for d in find_dependencies(edges, tid)]
            new_status = calculate_ticket_status(current, dep_statuses)
            if new_status != current:
                states[tid] = new_status
                changed = True
        if not changed:
            return states
    raise AssertionError("status fixpoint did not converge")


@given(graph=_acyclic_graph())
@settings(max_examples=250, deadline=None, derandomize=True)
def test_cascade_never_promotes_with_unsatisfied_dep(
    graph: tuple[list[str], dict[str, str], list[DepEdge]],
) -> None:
    ids, initial, edges = graph
    final = _run_status_fixpoint(initial, edges)

    # MAIN: every ready ticket has ALL its dependencies done.
    for tid in ids:
        if final[tid] == "ready":
            for dep in find_dependencies(edges, tid):
                assert final[dep] == "done", (
                    f"{tid} is 'ready' but dep {dep} is {final[dep]!r} (not done)"
                )

    # STICKY: an initially active/done ticket is never demoted.
    for tid in ids:
        if initial[tid] in _STICKY:
            assert final[tid] == initial[tid], f"{tid} {initial[tid]!r} -> {final[tid]!r} (sticky)"

    # Promotion only ever yields pending/ready: any active/done at the end started so.
    for tid in ids:
        if final[tid] in _STICKY:
            assert initial[tid] == final[tid], f"{tid} promoted into sticky status {final[tid]!r}"


# --------------------------------------------------------------------------- #
# Property 2 — the tree is acyclic except for the reported cycles             #
# --------------------------------------------------------------------------- #


@st.composite
def _any_graph(draw: st.DrawFn) -> tuple[list[str], list[TicketNode], list[DepEdge]]:
    """A random graph that MAY contain cycles and self-loops."""
    n = draw(st.integers(min_value=1, max_value=8))
    ids = [f"N{i:03d}" for i in range(n)]
    tickets = [
        TicketNode(
            id=tid,
            status=draw(st.sampled_from(_STATUSES)),
            estimated_minutes=draw(st.integers(min_value=0, max_value=120)),
            epic_id="E",
            epic_number=1,
            ticket_number=k + 1,
            title="T",
        )
        for k, tid in enumerate(ids)
    ]

    edges: list[DepEdge] = []
    seen: set[tuple[str, str]] = set()
    m = draw(st.integers(min_value=0, max_value=3 * n))
    for _ in range(m):
        a = ids[draw(st.integers(min_value=0, max_value=n - 1))]
        b = ids[draw(st.integers(min_value=0, max_value=n - 1))]  # may equal a (self-loop)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        edges.append(DepEdge(ticket_id=a, depends_on_id=b))
    return ids, tickets, edges


def _reaches_self(start: str, adj: dict[str, set[str]]) -> bool:
    """Can ``start`` get back to itself via a path of length >= 1? (cycle membership)."""
    stack = list(adj[start])
    visited: set[str] = set()
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adj[node])
    return False


def _is_acyclic(adj: dict[str, list[str]]) -> bool:
    """DFS three-colour cycle check over a directed adjacency map."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adj, WHITE)

    def visit(node: str) -> bool:
        colour[node] = GREY
        for nxt in adj.get(node, ()):
            if nxt not in colour:
                continue
            if colour[nxt] == GREY:
                return False
            if colour[nxt] == WHITE and not visit(nxt):
                return False
        colour[node] = BLACK
        return True

    return all(colour[node] != WHITE or visit(node) for node in adj)


@given(graph=_any_graph())
@settings(max_examples=250, deadline=None, derandomize=True)
def test_tree_acyclic_except_reported_cycles(
    graph: tuple[list[str], list[TicketNode], list[DepEdge]],
) -> None:
    ids, tickets, edges = graph
    epics = [EpicNode(id="E", epic_number=1, title="E", order=1)]
    tree = build_dependency_tree(
        "SPEC", epics, tickets, edges, generated_at="2026-06-26T00:00:00+00:00"
    )

    cycle_nodes: set[str] = set()
    for cyc in tree.cycles:
        cycle_nodes.update(cyc.cycle)

    # hasCircularDeps is exactly "there is at least one reported cycle".
    assert tree.summary.has_circular_deps == (len(tree.cycles) > 0)
    assert tree.summary.has_circular_deps == (len(cycle_nodes) > 0)

    # Reference oracle: a node is cyclic iff it can reach itself. This must equal the
    # set of nodes the Tarjan SCC engine reported as cyclic.
    adj: dict[str, set[str]] = {tid: set() for tid in ids}
    for e in edges:
        adj[e.ticket_id].add(e.depends_on_id)
    reference = {tid for tid in ids if _reaches_self(tid, adj)}
    assert reference == cycle_nodes

    # Every self-loop is captured.
    for e in edges:
        if e.ticket_id == e.depends_on_id:
            assert e.ticket_id in cycle_nodes

    # The tree's own edge set, restricted to non-cycle nodes, is acyclic.
    sub_adj: dict[str, list[str]] = {tid: [] for tid in ids if tid not in cycle_nodes}
    for edge in tree.edges:
        if edge.from_ not in cycle_nodes and edge.to not in cycle_nodes:
            sub_adj[edge.from_].append(edge.to)
    assert _is_acyclic(sub_adj)

    # The critical path is a simple path drawn only from non-cycle nodes.
    cp_ids = [node.id for node in tree.critical_path]
    assert len(cp_ids) == len(set(cp_ids))
    assert all(nid not in cycle_nodes for nid in cp_ids)


# --------------------------------------------------------------------------- #
# Property 3 — denormalized counts == SELECT COUNT ground truth               #
# --------------------------------------------------------------------------- #


def _expected_progress(completed: int, total: int) -> int:
    """Half-up percentage: ``floor(completed / total * 100 + 0.5)`` (0 when total 0)."""
    if total == 0:
        return 0
    return math.floor(completed / total * 100 + 0.5)


_SPEC_BUCKET = {
    SpecStatus.DRAFT.value: "draft_spec_count",
    SpecStatus.PLANNING.value: "planning_spec_count",
    SpecStatus.READY.value: "ready_spec_count",
    SpecStatus.IN_PROGRESS.value: "in_progress_spec_count",
    SpecStatus.READY_FOR_REVIEW.value: "in_review_spec_count",
    SpecStatus.IN_REVIEW.value: "in_review_spec_count",
    SpecStatus.REVIEWED.value: "in_review_spec_count",
    SpecStatus.DONE.value: "completed_spec_count",
}


@given(data=st.data())
@settings(
    max_examples=30,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_denormalized_counts_match_select_count(data: st.DataObject) -> None:
    spec_status_choices = [s.value for s in SpecStatus]
    ticket_status_choices = [s.value for s in TicketStatus]

    with tempfile.TemporaryDirectory() as tmp:
        engine = init_db(Path(tmp) / "props.db")
        try:
            factory = make_session_factory(engine)
            project_id = new_ulid()
            spec_ids: list[str] = []
            spec_status_by_id: dict[str, str] = {}

            with factory.begin() as session:
                session.add(Project(id=project_id, name="P"))
                n_specs = data.draw(st.integers(min_value=1, max_value=2), label="n_specs")
                for _ in range(n_specs):
                    spec_id = new_ulid()
                    spec_ids.append(spec_id)
                    status = data.draw(st.sampled_from(spec_status_choices))
                    spec_status_by_id[spec_id] = status
                    session.add(
                        Specification(
                            id=spec_id,
                            project_id=project_id,
                            title="S",
                            specification_type_id=None,
                            status=status,
                        )
                    )

                    spec_ticket_ids: list[str] = []
                    n_epics = data.draw(st.integers(min_value=0, max_value=3))
                    for e in range(n_epics):
                        epic_id = new_ulid()
                        session.add(
                            Epic(
                                id=epic_id,
                                specification_id=spec_id,
                                epic_number=e + 1,
                                title="E",
                                description="d",
                                objective="o",
                                order=e + 1,
                            )
                        )
                        n_tickets = data.draw(st.integers(min_value=0, max_value=4))
                        for _ in range(n_tickets):
                            tid = new_ulid()
                            spec_ticket_ids.append(tid)
                            session.add(
                                Ticket(
                                    id=tid,
                                    epic_id=epic_id,
                                    title="T",
                                    ticket_number=len(spec_ticket_ids),
                                    status=data.draw(st.sampled_from(ticket_status_choices)),
                                    estimated_minutes=data.draw(
                                        st.integers(min_value=0, max_value=120)
                                    ),
                                )
                            )

                    # Acyclic intra-spec dep edges: later ticket depends on earlier one.
                    n = len(spec_ticket_ids)
                    if n >= 2:
                        seen_edges: set[tuple[str, str]] = set()
                        n_edges = data.draw(st.integers(min_value=0, max_value=min(2 * n, 8)))
                        for _ in range(n_edges):
                            i = data.draw(st.integers(min_value=1, max_value=n - 1))
                            j = data.draw(st.integers(min_value=0, max_value=i - 1))
                            edge = (spec_ticket_ids[i], spec_ticket_ids[j])
                            if edge in seen_edges:
                                continue
                            seen_edges.add(edge)
                            session.add(
                                TicketDependency(
                                    id=new_ulid(), ticket_id=edge[0], depends_on_id=edge[1]
                                )
                            )

                    # Random blueprints + ticket refs (random subset per ticket).
                    bp_ids: list[str] = []
                    n_bp = data.draw(st.integers(min_value=0, max_value=3))
                    for _ in range(n_bp):
                        bp_id = new_ulid()
                        bp_ids.append(bp_id)
                        session.add(
                            Blueprint(
                                id=bp_id,
                                specification_id=spec_id,
                                category="diagram",
                                title="BP",
                                content="x",
                            )
                        )
                    if bp_ids:
                        ref_seen: set[tuple[str, str]] = set()
                        for tid in spec_ticket_ids:
                            for bp_id in bp_ids:
                                if data.draw(st.booleans()) and (tid, bp_id) not in ref_seen:
                                    ref_seen.add((tid, bp_id))
                                    session.add(
                                        TicketBlueprintRef(
                                            id=new_ulid(), ticket_id=tid, blueprint_id=bp_id
                                        )
                                    )

                recompute(session, spec_ids=spec_ids)

            with factory() as session:
                _assert_counts_match(session, project_id, spec_ids, spec_status_by_id)
        finally:
            engine.dispose()


def _count(session: Session, stmt: Any) -> int:
    value = session.scalar(stmt)
    return int(value or 0)


def _assert_counts_match(
    sess: Session,
    project_id: str,
    spec_ids: list[str],
    spec_status_by_id: dict[str, str],
) -> None:
    for spec_id in spec_ids:
        spec = sess.get(Specification, spec_id)
        assert spec is not None

        epics = list(
            sess.execute(select(Epic).where(Epic.specification_id == spec_id)).scalars()
        )
        assert spec.epic_count == len(epics)

        # ---- per-epic counts ---------------------------------------------- #
        for epic in epics:
            eid = epic.id
            base = select(func.count()).select_from(Ticket).where(Ticket.epic_id == eid)
            total = _count(sess, base)
            done = _count(sess, base.where(Ticket.status == "done"))
            pending = _count(sess, base.where(Ticket.status == "pending"))
            ready = _count(sess, base.where(Ticket.status == "ready"))
            active = _count(sess, base.where(Ticket.status == "active"))
            est = _count(
                sess,
                select(func.coalesce(func.sum(Ticket.estimated_minutes), 0)).where(
                    Ticket.epic_id == eid
                ),
            )
            assert epic.ticket_count == total
            assert epic.completed_ticket_count == done
            assert epic.pending_ticket_count == pending
            assert epic.ready_ticket_count == ready
            assert epic.active_ticket_count == active
            assert epic.estimated_minutes == est
            assert epic.progress == _expected_progress(done, total)

            # intra-epic dependency edges (both endpoints in this epic).
            t1 = aliased(Ticket)
            t2 = aliased(Ticket)
            epic_deps = _count(
                sess,
                select(func.count())
                .select_from(TicketDependency)
                .join(t1, t1.id == TicketDependency.ticket_id)
                .join(t2, t2.id == TicketDependency.depends_on_id)
                .where(t1.epic_id == eid, t2.epic_id == eid),
            )
            assert epic.dependency_count == epic_deps

            # distinct blueprint ids across this epic's tickets' refs.
            epic_bp = _count(
                sess,
                select(func.count(func.distinct(TicketBlueprintRef.blueprint_id)))
                .select_from(TicketBlueprintRef)
                .join(Ticket, Ticket.id == TicketBlueprintRef.ticket_id)
                .where(Ticket.epic_id == eid),
            )
            assert epic.blueprint_count == epic_bp

        # ---- per-ticket dependency / blueprint counts --------------------- #
        tickets = list(
            sess.execute(
                select(Ticket)
                .join(Epic, Epic.id == Ticket.epic_id)
                .where(Epic.specification_id == spec_id)
            ).scalars()
        )
        for ticket in tickets:
            incoming = _count(
                sess,
                select(func.count())
                .select_from(TicketDependency)
                .where(TicketDependency.depends_on_id == ticket.id),
            )
            outgoing = _count(
                sess,
                select(func.count())
                .select_from(TicketDependency)
                .where(TicketDependency.ticket_id == ticket.id),
            )
            ticket_bp = _count(
                sess,
                select(func.count())
                .select_from(TicketBlueprintRef)
                .where(TicketBlueprintRef.ticket_id == ticket.id),
            )
            assert ticket.incoming_dep_count == incoming
            assert ticket.outgoing_dep_count == outgoing
            assert ticket.blueprint_count == ticket_bp

        # ---- spec-level rollups ------------------------------------------- #
        spec_tickets = (
            select(func.count())
            .select_from(Ticket)
            .join(Epic, Epic.id == Ticket.epic_id)
            .where(Epic.specification_id == spec_id)
        )
        s_total = _count(sess, spec_tickets)
        s_done = _count(sess, spec_tickets.where(Ticket.status == "done"))
        s_pending = _count(sess, spec_tickets.where(Ticket.status == "pending"))
        s_ready = _count(sess, spec_tickets.where(Ticket.status == "ready"))
        s_active = _count(sess, spec_tickets.where(Ticket.status == "active"))
        assert spec.ticket_count == s_total
        assert spec.completed_ticket_count == s_done
        assert spec.pending_ticket_count == s_pending
        assert spec.ready_ticket_count == s_ready
        assert spec.active_ticket_count == s_active
        assert spec.progress == _expected_progress(s_done, s_total)

        spec_deps = _count(
            sess,
            select(func.count())
            .select_from(TicketDependency)
            .join(Ticket, Ticket.id == TicketDependency.ticket_id)
            .join(Epic, Epic.id == Ticket.epic_id)
            .where(Epic.specification_id == spec_id),
        )
        assert spec.dependency_count == spec_deps

        spec_bp = _count(
            sess,
            select(func.count())
            .select_from(Blueprint)
            .where(Blueprint.specification_id == spec_id),
        )
        assert spec.blueprint_count == spec_bp

        # completed-epic bucket: an epic is "completed" iff every ticket is done
        # and it has at least one ticket (progress == 100).
        expected_completed_epics = sum(
            1 for ep in epics if ep.ticket_count > 0 and ep.completed_ticket_count == ep.ticket_count
        )
        assert spec.completed_epic_count == expected_completed_epics
        assert (
            spec.todo_epic_count + spec.in_progress_epic_count + spec.completed_epic_count
            == spec.epic_count
        )

    # ---- project per-status spec buckets + rollups ------------------------ #
    project = sess.get(Project, project_id)
    assert project is not None
    assert project.spec_count == len(spec_ids)

    expected_buckets = {
        "draft_spec_count": 0,
        "planning_spec_count": 0,
        "ready_spec_count": 0,
        "in_progress_spec_count": 0,
        "in_review_spec_count": 0,
        "completed_spec_count": 0,
    }
    for status in spec_status_by_id.values():
        expected_buckets[_SPEC_BUCKET[status]] += 1
    for column, expected in expected_buckets.items():
        assert getattr(project, column) == expected
    assert sum(expected_buckets.values()) == project.spec_count

    proj_epics = _count(
        sess,
        select(func.count())
        .select_from(Epic)
        .join(Specification, Specification.id == Epic.specification_id)
        .where(Specification.project_id == project_id),
    )
    proj_tickets = (
        select(func.count())
        .select_from(Ticket)
        .join(Epic, Epic.id == Ticket.epic_id)
        .join(Specification, Specification.id == Epic.specification_id)
        .where(Specification.project_id == project_id)
    )
    assert project.epic_count == proj_epics
    assert project.ticket_count == _count(sess, proj_tickets)
    assert project.completed_ticket_count == _count(
        sess, proj_tickets.where(Ticket.status == "done")
    )
