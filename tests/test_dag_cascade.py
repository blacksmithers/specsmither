"""Unit tests for the single-hop cascade engine (``specsmither.dag.cascade``).

These explicitly exercise the determinism traps that change outputs versus the
TS engine: single-hop (NOT transitive) promotion, the unknown-id-is-a-blocker
rule, first-seen dedup ordering of dependents, and the active/done never-demoted
invariant.
"""

from __future__ import annotations

from specsmither.dag.cascade import (
    CascadeTransition,
    cascade_on_completed,
    cascade_on_dependency_change,
    cascade_on_uncompleted,
    check_dependencies_completed,
    find_dependencies,
    find_dependents,
)
from specsmither.dag.types import DepEdge


def _edge(ticket_id: str, depends_on_id: str) -> DepEdge:
    return DepEdge(ticket_id=ticket_id, depends_on_id=depends_on_id)


# --------------------------------------------------------------------------- #
# find_dependents / find_dependencies                                         #
# --------------------------------------------------------------------------- #


def test_find_dependents_dedup_first_seen_order() -> None:
    # Two edges make the same ticket depend on `a` twice; `c` depends on `a`
    # once. First-seen insertion order is preserved and duplicates dropped.
    edges = [_edge("b", "a"), _edge("c", "a"), _edge("b", "a")]
    assert find_dependents(edges, "a") == ["b", "c"]


def test_find_dependents_only_direct_edges() -> None:
    edges = [_edge("b", "a"), _edge("c", "b")]
    assert find_dependents(edges, "a") == ["b"]  # c depends on b, not a


def test_find_dependencies_dedup_first_seen_order() -> None:
    edges = [_edge("d", "a"), _edge("d", "b"), _edge("d", "a")]
    assert find_dependencies(edges, "d") == ["a", "b"]


# --------------------------------------------------------------------------- #
# check_dependencies_completed                                                #
# --------------------------------------------------------------------------- #


def test_check_no_dependencies_is_vacuously_completed() -> None:
    result = check_dependencies_completed("x", {"x": "pending"}, [])
    assert result.all_completed is True
    assert result.blockers == []
    assert result.missing == []


def test_check_unknown_dep_is_blocker_and_missing() -> None:
    # `a` is absent from states entirely → it is BOTH a blocker and missing.
    edges = [_edge("b", "a")]
    result = check_dependencies_completed("b", {"b": "pending"}, edges)
    assert result.all_completed is False
    assert result.blockers == ["a"]
    assert result.missing == ["a"]


def test_check_incomplete_dep_is_blocker_not_missing() -> None:
    edges = [_edge("b", "a")]
    result = check_dependencies_completed("b", {"a": "active", "b": "pending"}, edges)
    assert result.all_completed is False
    assert result.blockers == ["a"]
    assert result.missing == []


def test_check_all_done_is_completed() -> None:
    edges = [_edge("c", "a"), _edge("c", "b")]
    states = {"a": "done", "b": "done", "c": "pending"}
    result = check_dependencies_completed("c", states, edges)
    assert result.all_completed is True
    assert result.blockers == []
    assert result.missing == []


# --------------------------------------------------------------------------- #
# cascade_on_completed — THE chain.test.ts single-hop case                    #
# --------------------------------------------------------------------------- #


def test_on_completed_is_single_hop_not_transitive() -> None:
    # A→B→C: B depends on A, C depends on B. Only A is done.
    # Single-hop: completing A promotes ONLY B. C is NOT touched (its blocker B
    # is still pending, and C is not even a direct dependent of A).
    edges = [_edge("b", "a"), _edge("c", "b")]
    states = {"a": "done", "b": "pending", "c": "pending"}
    transitions = cascade_on_completed(states, edges, "a")
    assert transitions == [
        CascadeTransition(
            ticket_id="b",
            from_status="pending",
            to_status="ready",
            reason="deps-completed",
        )
    ]


def test_on_completed_guard_root_not_done() -> None:
    edges = [_edge("b", "a")]
    states = {"a": "pending", "b": "pending"}
    assert cascade_on_completed(states, edges, "a") == []


def test_on_completed_skips_dependent_with_other_incomplete_dep() -> None:
    # Diamond floor: D depends on B and C. A done, B done, C still pending → D
    # is NOT promoted because not all of its deps are done.
    edges = [_edge("d", "b"), _edge("d", "c")]
    states = {"b": "done", "c": "pending", "d": "pending"}
    # b just completed; its only dependent is d, but d still blocked by c.
    assert cascade_on_completed(states, edges, "b") == []


def test_on_completed_only_promotes_pending_dependents() -> None:
    # A done; dependent b is already active → not promoted (only pending).
    edges = [_edge("b", "a")]
    states = {"a": "done", "b": "active"}
    assert cascade_on_completed(states, edges, "a") == []


# --------------------------------------------------------------------------- #
# cascade_on_uncompleted                                                       #
# --------------------------------------------------------------------------- #


def test_on_uncompleted_demotes_ready_dependent() -> None:
    edges = [_edge("b", "a")]
    states = {"a": "active", "b": "ready"}  # a left 'done'
    transitions = cascade_on_uncompleted(states, edges, "a")
    assert transitions == [
        CascadeTransition(
            ticket_id="b",
            from_status="ready",
            to_status="pending",
            reason="dep-reverted",
        )
    ]


def test_on_uncompleted_guard_root_still_done() -> None:
    edges = [_edge("b", "a")]
    states = {"a": "done", "b": "ready"}
    assert cascade_on_uncompleted(states, edges, "a") == []


def test_on_uncompleted_never_demotes_active_or_done() -> None:
    # Two dependents of a: one active, one done — neither is demoted.
    edges = [_edge("b", "a"), _edge("c", "a")]
    states = {"a": "pending", "b": "active", "c": "done"}
    assert cascade_on_uncompleted(states, edges, "a") == []


# --------------------------------------------------------------------------- #
# cascade_on_dependency_change — add / remove                                  #
# --------------------------------------------------------------------------- #


def test_on_dependency_change_add_incomplete_blocker_demotes_ready() -> None:
    # Edge b→a ADDED; a is incomplete → ready b reverts to pending.
    edges = [_edge("b", "a")]
    states = {"a": "pending", "b": "ready"}
    transitions = cascade_on_dependency_change(states, edges, "b")
    assert transitions == [
        CascadeTransition(
            ticket_id="b",
            from_status="ready",
            to_status="pending",
            reason="dep-reverted",
        )
    ]


def test_on_dependency_change_remove_last_blocker_promotes_pending() -> None:
    # The last incomplete blocker was REMOVED → b now has no deps and is
    # pending → promote to ready.
    edges: list[DepEdge] = []
    states = {"b": "pending"}
    transitions = cascade_on_dependency_change(states, edges, "b")
    assert transitions == [
        CascadeTransition(
            ticket_id="b",
            from_status="pending",
            to_status="ready",
            reason="deps-completed",
        )
    ]


def test_on_dependency_change_never_reblocks_active() -> None:
    # active ticket with an incomplete dep is NOT re-blocked.
    edges = [_edge("b", "a")]
    states = {"a": "pending", "b": "active"}
    assert cascade_on_dependency_change(states, edges, "b") == []


def test_on_dependency_change_done_is_untouched() -> None:
    edges = [_edge("b", "a")]
    states = {"a": "pending", "b": "done"}
    assert cascade_on_dependency_change(states, edges, "b") == []


def test_on_dependency_change_ready_with_all_done_no_change() -> None:
    # ready ticket whose deps are all done stays ready (no spurious demote).
    edges = [_edge("b", "a")]
    states = {"a": "done", "b": "ready"}
    assert cascade_on_dependency_change(states, edges, "b") == []


def test_on_dependency_change_pending_with_incomplete_dep_no_change() -> None:
    edges = [_edge("b", "a")]
    states = {"a": "pending", "b": "pending"}
    assert cascade_on_dependency_change(states, edges, "b") == []
