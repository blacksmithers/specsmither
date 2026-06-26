"""Determinism-trap tests for ``specsmither.dag.critical_path``.

Each test pins one TS-parity invariant that, if it drifted, would break the
byte-for-byte golden comparison against the SpecForge engine.
"""

from __future__ import annotations

from specsmither.dag.critical_path import (
    UNNUMBERED_PLACEHOLDER,
    CriticalPathNode,
    compute_critical_path,
    compute_max_blocker_depth,
    find_cycles,
    lex_less,
    round_half_up,
)
from specsmither.dag.types import DepEdge, TicketNode


def _path_ids(nodes: tuple[CriticalPathNode, ...]) -> list[str]:
    return [n.id for n in nodes]


# --- (a) lexLess tie-break: equal-minute paths -> lexicographically smaller -----


def test_lex_less_semantics() -> None:
    # First differing element decides.
    assert lex_less(["a", "y"], ["x"]) is True
    assert lex_less(["x"], ["a", "y"]) is False
    # Shared prefix -> shorter list wins.
    assert lex_less(["a"], ["a", "b"]) is True
    assert lex_less(["a", "b"], ["a"]) is False
    # Equal lists are not "less".
    assert lex_less(["a"], ["a"]) is False


def test_equal_minute_paths_pick_lexicographically_smaller() -> None:
    # m depends on x (leaf, weight 10) and y (-> a, 5 + 5 = 10). Both candidate
    # sub-paths tie at 10 minutes, so lexLess must pick [a, y] over [x] because
    # "a" < "x" — and it must do so even though x is iterated first (deps sorted).
    tickets = [
        TicketNode(id="a", status="pending", estimated_minutes=5),
        TicketNode(id="m", status="pending", estimated_minutes=20),
        TicketNode(id="x", status="pending", estimated_minutes=10),
        TicketNode(id="y", status="pending", estimated_minutes=5),
    ]
    edges = [
        DepEdge(ticket_id="m", depends_on_id="x"),
        DepEdge(ticket_id="m", depends_on_id="y"),
        DepEdge(ticket_id="y", depends_on_id="a"),
    ]
    result = compute_critical_path(tickets, edges)
    assert _path_ids(result.path) == ["a", "y", "m"]
    assert result.length == 3
    assert result.minutes == 30  # 5 + 5 + 20


# --- (b) 0/None estimate weighs 1 in selection but displays via `?? 60` ----------


def test_missing_estimate_weighs_one_but_displays_sixty() -> None:
    result = compute_critical_path([TicketNode(id="n", status="pending")], [])
    assert _path_ids(result.path) == ["n"]
    # Selection weight is 1 (NOT 60): the summed-weight minutes is 1.
    assert result.minutes == 1
    # Display default for a *missing* estimate is 60.
    assert result.path[0].estimated_minutes == 60


def test_zero_estimate_weighs_one_but_displays_zero() -> None:
    # The TS `?? 60` only defaults null/undefined, not 0: a literal 0 renders 0.
    result = compute_critical_path(
        [TicketNode(id="z", status="pending", estimated_minutes=0)], []
    )
    assert result.minutes == 1  # weight = 1
    assert result.path[0].estimated_minutes == 0  # display = 0, NOT 60


def test_render_guards_for_missing_number_and_title() -> None:
    result = compute_critical_path([TicketNode(id="t", status="pending")], [])
    assert result.path[0].ticket_number == 0
    assert result.path[0].title == UNNUMBERED_PLACEHOLDER


# --- (c) criticalPathMinutes is half-up, not banker's ----------------------------


def test_round_half_up_diverges_from_bankers() -> None:
    # Python's built-in round is banker's; these are the divergent cases.
    assert round(0.5) == 0 and round_half_up(0.5) == 1
    assert round(2.5) == 2 and round_half_up(2.5) == 3
    # Agreement cases (sanity).
    assert round_half_up(1.5) == 2
    assert round_half_up(2.4) == 2
    assert round_half_up(2.6) == 3
    # Negative half rounds toward +inf (JS Math.round(-2.5) == -2).
    assert round_half_up(-2.5) == -2
    assert round_half_up(-0.5) == 0


# --- (d) find_cycles: 2-node SCC + self-loops, each sorted, result sorted --------


def test_find_cycles_scc_and_self_loops_sorted() -> None:
    # b: self-loop; p<->q: 2-node SCC; s: self-loop; a: not in any cycle.
    # Inputs intentionally unordered to prove the output ordering is derived.
    edges = [
        DepEdge(ticket_id="s", depends_on_id="s"),
        DepEdge(ticket_id="q", depends_on_id="p"),
        DepEdge(ticket_id="a", depends_on_id="p"),
        DepEdge(ticket_id="p", depends_on_id="q"),
        DepEdge(ticket_id="b", depends_on_id="b"),
    ]
    cycles = find_cycles(["s", "a", "q", "p", "b"], edges)
    # Each cycle sorted internally; whole result sorted by cycle[0].
    assert cycles == [["b"], ["p", "q"], ["s"]]


def test_find_cycles_accepts_ticket_nodes() -> None:
    tickets = [
        TicketNode(id="q", status="pending"),
        TicketNode(id="p", status="pending"),
    ]
    edges = [
        DepEdge(ticket_id="p", depends_on_id="q"),
        DepEdge(ticket_id="q", depends_on_id="p"),
    ]
    assert find_cycles(tickets, edges) == [["p", "q"]]


def test_find_cycles_empty_when_acyclic() -> None:
    edges = [DepEdge(ticket_id="b", depends_on_id="a")]
    assert find_cycles(["a", "b"], edges) == []


# --- (e) blocker depth: edge-counted incomplete chain; done link terminates ------


def test_blocker_depth_counts_incomplete_chain_edges() -> None:
    # t3 -> t2 -> t1 -> t0, all incomplete: 3 edges -> depth 3.
    tickets = [
        TicketNode(id="t0", status="pending"),
        TicketNode(id="t1", status="pending"),
        TicketNode(id="t2", status="pending"),
        TicketNode(id="t3", status="pending"),
    ]
    edges = [
        DepEdge(ticket_id="t1", depends_on_id="t0"),
        DepEdge(ticket_id="t2", depends_on_id="t1"),
        DepEdge(ticket_id="t3", depends_on_id="t2"),
    ]
    assert compute_max_blocker_depth(tickets, edges) == 3


def test_blocker_depth_done_link_terminates_chain() -> None:
    # Same chain but t1 is done: the (t2 -> t1) edge is dropped, so the longest
    # incomplete chain is just (t3 -> t2) -> depth 1.
    tickets = [
        TicketNode(id="t0", status="pending"),
        TicketNode(id="t1", status="done"),
        TicketNode(id="t2", status="pending"),
        TicketNode(id="t3", status="pending"),
    ]
    edges = [
        DepEdge(ticket_id="t1", depends_on_id="t0"),
        DepEdge(ticket_id="t2", depends_on_id="t1"),
        DepEdge(ticket_id="t3", depends_on_id="t2"),
    ]
    assert compute_max_blocker_depth(tickets, edges) == 1


def test_blocker_depth_excludes_cycle_nodes() -> None:
    tickets = [
        TicketNode(id="t0", status="pending"),
        TicketNode(id="t1", status="pending"),
        TicketNode(id="t2", status="pending"),
        TicketNode(id="t3", status="pending"),
    ]
    edges = [
        DepEdge(ticket_id="t1", depends_on_id="t0"),
        DepEdge(ticket_id="t2", depends_on_id="t1"),
        DepEdge(ticket_id="t3", depends_on_id="t2"),
    ]
    # Excluding t3 drops the deepest link: longest chain is t2 -> t1 -> t0 = 2.
    assert compute_max_blocker_depth(tickets, edges, exclude=frozenset({"t3"})) == 2


def test_blocker_depth_residual_cycle_terminates() -> None:
    # x <-> y, neither excluded: the visiting guard makes the back-edge return 0
    # so traversal terminates (no infinite recursion). Max edge-count is 2.
    tickets = [
        TicketNode(id="x", status="pending"),
        TicketNode(id="y", status="pending"),
    ]
    edges = [
        DepEdge(ticket_id="x", depends_on_id="y"),
        DepEdge(ticket_id="y", depends_on_id="x"),
    ]
    assert compute_max_blocker_depth(tickets, edges) == 2


# --- critical-path exclusion + ordering sanity -----------------------------------


def test_critical_path_excludes_done_and_cycle_nodes() -> None:
    tickets = [
        TicketNode(id="a", status="done", estimated_minutes=100),
        TicketNode(id="b", status="pending", estimated_minutes=30),
        TicketNode(id="c", status="pending", estimated_minutes=10),
    ]
    edges = [DepEdge(ticket_id="c", depends_on_id="b")]
    # Exclude the done node 'a'; the path is b -> c (30 + 10 = 40).
    result = compute_critical_path(tickets, edges, exclude=frozenset({"a"}))
    assert _path_ids(result.path) == ["b", "c"]
    assert result.minutes == 40
