"""Determinism-trap tests for ``specsmither.dag.tree``.

Each test pins one TS-parity invariant of ``build-dependency-tree.ts`` /
``compute-tree-update.ts`` that, if it drifted, would break the byte-for-byte
golden comparison against the SpecForge engine: derived ticket fields, summary
histogram, ready/blocked ordering, critical-path done/cycle exclusion, the
``?? 60`` display default, deduped/sorted adjacency, and the change-gate that
suppresses version bumps when nothing structural moved.
"""

from __future__ import annotations

from specsmither.dag.tree import (
    DependencyTree,
    build_dependency_tree,
    compute_tree_update,
)
from specsmither.dag.types import DepEdge, EpicNode, TicketNode

# A pinned timestamp so two builds differ ONLY by generated_at.
_TS1 = "2026-01-01T00:00:00+00:00"
_TS2 = "2026-02-02T00:00:00+00:00"


def _epics() -> list[EpicNode]:
    return [
        EpicNode(id="e1", epic_number=1, title="Epic One"),
        EpicNode(id="e2", epic_number=2, title="Epic Two"),
    ]


def _tickets() -> list[TicketNode]:
    # a(done) <- b(pending) <- c(pending) chain; d(active) isolated.
    return [
        TicketNode(id="a", status="done", estimated_minutes=30, epic_id="e1", ticket_number=1,
                   title="A"),
        TicketNode(id="b", status="pending", estimated_minutes=20, epic_id="e1", ticket_number=2,
                   title="B"),
        TicketNode(id="c", status="pending", estimated_minutes=40, epic_id="e2", ticket_number=3,
                   title="C"),
        TicketNode(id="d", status="active", estimated_minutes=None, epic_id="e2", ticket_number=4,
                   title="D"),
    ]


def _edges() -> list[DepEdge]:
    return [DepEdge(ticket_id="b", depends_on_id="a"), DepEdge(ticket_id="c", depends_on_id="b")]


def _build(previous_version: int | None = None) -> DependencyTree:
    return build_dependency_tree(
        "spec-1", _epics(), _tickets(), _edges(),
        generated_at=_TS1, previous_version=previous_version,
    )


# --- (a) derived ticket fields: unsatisfiedDeps / blockerCount / isReady --------


def test_derived_ticket_fields() -> None:
    tree = _build()
    a, b, c, d = (tree.tickets[i] for i in ("a", "b", "c", "d"))

    # 'a' is done: a satisfied dep, never blocks, never ready.
    assert a.unsatisfied_deps == () and a.blocker_count == 0 and a.is_ready is False
    assert a.blocks == ("b",)  # reverse edge

    # 'b' depends on 'a' which is done -> satisfied -> ready.
    assert b.dependencies == ("a",)
    assert b.unsatisfied_deps == () and b.blocker_count == 0 and b.is_ready is True
    assert b.blocks == ("c",)

    # 'c' depends on 'b' which is pending -> b is an unsatisfied blocker.
    assert c.dependencies == ("b",)
    assert c.unsatisfied_deps == ("b",) and c.blocker_count == 1 and c.is_ready is False

    # 'd' is active with no deps: is_ready True but NOT a pending/ready bucket.
    assert d.is_ready is True


# --- (b) summary histogram + ready/blocked membership ----------------------------


def test_summary_counts_and_membership() -> None:
    s = _build().summary
    assert s.total_tickets == 4
    tbs = s.tickets_by_status
    assert (tbs.pending, tbs.ready, tbs.active, tbs.done) == (2, 0, 1, 1)
    # readyTickets = is_ready AND status in {pending, ready}: only 'b'
    # ('d' is is_ready but active, so excluded).
    assert s.ready_tickets == ("b",)
    # blockedTickets = blocker_count > 0 AND status != done: only 'c'.
    assert s.blocked_tickets == ("c",)
    assert s.has_circular_deps is False


def test_ready_and_blocked_sorted_by_ticket_number_then_id() -> None:
    # Two ready tickets whose ticket_number ordering disagrees with id ordering:
    # id "z" has ticket_number 1, id "a" has ticket_number 2 -> z sorts first.
    epics = [EpicNode(id="e1", epic_number=1, title="E")]
    tickets = [
        TicketNode(id="z", status="ready", epic_id="e1", ticket_number=1, title="Z"),
        TicketNode(id="a", status="pending", epic_id="e1", ticket_number=2, title="A"),
    ]
    s = build_dependency_tree("s", epics, tickets, [], generated_at=_TS1).summary
    assert s.ready_tickets == ("z", "a")  # ticket_number 1 < 2, NOT id order


# --- (c) critical path excludes done + cycle nodes; length/minutes ----------------


def test_critical_path_excludes_done_nodes() -> None:
    tree = _build()
    path_ids = [n.id for n in tree.critical_path]
    assert path_ids == ["b", "c"]  # 'a' (done) dropped, 'd' (1 min) not longest
    assert tree.summary.critical_path_length == 2
    assert tree.summary.critical_path_minutes == 60  # 20 + 40
    assert tree.summary.max_blocker_depth == 1  # c -> b (incomplete); a is satisfied
    # The materialized scalars are surfaced at the tree level too.
    assert tree.critical_path_length == 2
    assert tree.max_blocker_depth == 1


def test_critical_path_and_blocker_depth_exclude_cycle_nodes() -> None:
    epics = [EpicNode(id="e1", epic_number=1, title="E")]
    tickets = [
        TicketNode(id="x", status="pending", estimated_minutes=10, epic_id="e1", ticket_number=1),
        TicketNode(id="y", status="pending", estimated_minutes=10, epic_id="e1", ticket_number=2),
        TicketNode(id="z", status="pending", estimated_minutes=10, epic_id="e1", ticket_number=3),
    ]
    edges = [
        DepEdge(ticket_id="x", depends_on_id="y"),
        DepEdge(ticket_id="y", depends_on_id="x"),  # x<->y cycle
        DepEdge(ticket_id="z", depends_on_id="x"),
    ]
    tree = build_dependency_tree("s", epics, tickets, edges, generated_at=_TS1)
    assert tree.summary.has_circular_deps is True
    assert tree.cycles == build_dependency_tree("s", epics, tickets, edges,
                                                 generated_at=_TS2).cycles
    assert len(tree.cycles) == 1 and tree.cycles[0].cycle == ("x", "y")  # sorted ids
    # x, y are excluded from the path; only z survives.
    assert [n.id for n in tree.critical_path] == ["z"]
    # z's lone blocker 'x' is a cycle node -> excluded -> depth 0.
    assert tree.summary.max_blocker_depth == 0


# --- (d) the `?? 60` display default vs the 0->1 selection weight -----------------


def test_missing_estimate_renders_60_but_weighs_1() -> None:
    # Single ticket, estimate undefined: it IS the critical path (length 1). Its
    # DP weight is 1 (None -> 1) so criticalPathMinutes == 1, but the rendered
    # node defaults the missing estimate to 60 (TS `?? 60`).
    tickets = [TicketNode(id="solo", status="pending", estimated_minutes=None,
                          epic_id="e1", ticket_number=1)]
    tree = build_dependency_tree("s", [EpicNode(id="e1", epic_number=1)], tickets, [],
                                 generated_at=_TS1)
    assert tree.summary.critical_path_minutes == 1
    assert tree.critical_path[0].estimated_minutes == 60
    # estimatedMinutesTotal also applies the ?? 60 to the missing estimate.
    assert tree.summary.estimated_minutes_total == 60


def test_zero_estimate_renders_zero_not_60() -> None:
    # A literal 0 must NOT become 60 (only undefined/None does).
    tickets = [TicketNode(id="z", status="pending", estimated_minutes=0,
                          epic_id="e1", ticket_number=1)]
    tree = build_dependency_tree("s", [EpicNode(id="e1", epic_number=1)], tickets, [],
                                 generated_at=_TS1)
    assert tree.critical_path[0].estimated_minutes == 0
    assert tree.summary.estimated_minutes_total == 0
    assert tree.summary.critical_path_minutes == 1  # 0 still weighs 1 in selection


def test_estimated_minutes_total_mixes_defaults() -> None:
    tickets = [
        TicketNode(id="a", status="pending", estimated_minutes=0, epic_id="e1", ticket_number=1),
        TicketNode(id="b", status="pending", estimated_minutes=None, epic_id="e1",
                   ticket_number=2),
        TicketNode(id="c", status="pending", estimated_minutes=5, epic_id="e1", ticket_number=3),
    ]
    tree = build_dependency_tree("s", [EpicNode(id="e1", epic_number=1)], tickets, [],
                                 generated_at=_TS1)
    assert tree.summary.estimated_minutes_total == 65  # 0 + 60 + 5


# --- (e) deduped/sorted adjacency, reverse edges, sorted TreeEdge list -----------


def test_dependencies_and_edges_deduped_and_sorted() -> None:
    epics = [EpicNode(id="e1", epic_number=1)]
    tickets = [
        TicketNode(id="a", status="done", epic_id="e1", ticket_number=1),
        TicketNode(id="m", status="pending", epic_id="e1", ticket_number=2),
        TicketNode(id="z", status="done", epic_id="e1", ticket_number=3),
    ]
    # m depends on z then a, with a duplicate edge; expect deduped + sorted (a, z).
    edges = [
        DepEdge(ticket_id="m", depends_on_id="z"),
        DepEdge(ticket_id="m", depends_on_id="a"),
        DepEdge(ticket_id="m", depends_on_id="z"),  # duplicate
    ]
    tree = build_dependency_tree("s", epics, tickets, edges, generated_at=_TS1)
    assert tree.tickets["m"].dependencies == ("a", "z")
    # Reverse edges: both a and z block m.
    assert tree.tickets["a"].blocks == ("m",)
    assert tree.tickets["z"].blocks == ("m",)
    # TreeEdge list deduped + sorted by (from, to).
    assert [(e.from_, e.to) for e in tree.edges] == [("m", "a"), ("m", "z")]


def test_epic_ticket_ids_sorted_by_ticket_number_then_id() -> None:
    epics = [EpicNode(id="e1", epic_number=1)]
    # id order (q < r) disagrees with ticket_number order (r=1 < q=2).
    tickets = [
        TicketNode(id="q", status="pending", epic_id="e1", ticket_number=2),
        TicketNode(id="r", status="pending", epic_id="e1", ticket_number=1),
    ]
    tree = build_dependency_tree("s", epics, tickets, [], generated_at=_TS1)
    assert tree.epics[0].ticket_ids == ("r", "q")  # ticket_number 1 before 2


def test_missing_numbers_and_titles_guarded() -> None:
    epics = [EpicNode(id="e1", epic_number=None, title=None)]
    tickets = [TicketNode(id="t", status="pending", epic_id="e1", ticket_number=None, title="")]
    tree = build_dependency_tree("s", epics, tickets, [], generated_at=_TS1)
    assert tree.epics[0].epic_number == 0 and tree.epics[0].title == "<unnumbered>"
    tk = tree.tickets["t"]
    assert tk.ticket_number == 0 and tk.title == "<unnumbered>"
    assert tk.epic_number == 0  # epic_number None -> 0


# --- (f) version auto-increment + compute_tree_update gate ------------------------


def test_version_auto_increments_from_previous() -> None:
    assert _build().version == 1  # previous_version None -> 0 + 1
    assert _build(previous_version=0).version == 1  # 0 + 1 (?? semantics, not `or`)
    assert _build(previous_version=5).version == 6


def test_compute_tree_update_no_previous_is_change() -> None:
    tree = _build()
    changed, result = compute_tree_update(None, tree)
    assert changed is True and result is tree


def test_compute_tree_update_identical_rebuild_no_version_bump() -> None:
    # Same inputs, different generated_at + a bumped version -> NOT a change.
    prev = build_dependency_tree("spec-1", _epics(), _tickets(), _edges(),
                                 generated_at=_TS1, previous_version=0)
    rebuilt = build_dependency_tree("spec-1", _epics(), _tickets(), _edges(),
                                    generated_at=_TS2, previous_version=prev.version)
    assert prev.generated_at != rebuilt.generated_at and rebuilt.version == 2
    changed, result = compute_tree_update(prev, rebuilt)
    assert changed is False
    assert result is prev and result.version == 1  # gate keeps the old version


def test_compute_tree_update_status_change_bumps_version() -> None:
    prev = build_dependency_tree("spec-1", _epics(), _tickets(), _edges(),
                                 generated_at=_TS1, previous_version=0)
    # Flip 'b' pending -> done: changes status/isReady/blockerCount on b and c.
    mutated = _tickets()
    mutated[1] = TicketNode(id="b", status="done", estimated_minutes=20, epic_id="e1",
                            ticket_number=2, title="B")
    current = build_dependency_tree("spec-1", _epics(), mutated, _edges(),
                                    generated_at=_TS2, previous_version=prev.version)
    changed, result = compute_tree_update(prev, current)
    assert changed is True
    assert result is current and result.version == 2


def test_compute_tree_update_ignores_estimate_only_change() -> None:
    # An estimate change shifts criticalPathMinutes/estimatedMinutesTotal but NOT
    # any gated field -> the gate must report no change and keep the old version.
    prev = build_dependency_tree("spec-1", _epics(), _tickets(), _edges(),
                                 generated_at=_TS1, previous_version=0)
    reestimated = _tickets()
    reestimated[1] = TicketNode(id="b", status="pending", estimated_minutes=999, epic_id="e1",
                                ticket_number=2, title="B")
    current = build_dependency_tree("spec-1", _epics(), reestimated, _edges(),
                                    generated_at=_TS2, previous_version=prev.version)
    assert current.summary.critical_path_minutes != prev.summary.critical_path_minutes
    changed, result = compute_tree_update(prev, current)
    assert changed is False and result is prev and result.version == 1
