"""Unit tests for the pure count derivations (``specsmither.rollups.counts``).

Exercises the verified count semantics from recon A5 §4: the unified half-up
progress formula, ``deriveEpicStatus`` across all three derived statuses, the
spec/project bucket partitions (incl. the three review statuses folding into the
single ``in_review`` bucket), the per-edge dependency counts (in/out, spec-once,
epic-only-if-intra), and the epic distinct-blueprint count.
"""

from __future__ import annotations

from specsmither.dag.types import DepEdge, TicketNode
from specsmither.domain.enums import EpicStatus, SpecStatus, TicketStatus
from specsmither.rollups.counts import (
    BlueprintRef,
    EpicCounts,
    EpicCountsView,
    SpecCountsView,
    count_distinct_blueprint_ids,
    derive_blueprint_counts,
    derive_dependency_counts,
    derive_epic_counts,
    derive_epic_status,
    derive_project_counts,
    derive_spec_counts,
    has_epic_counts_changed,
    progress_pct,
)


def _ticket(status: str, *, id: str = "t", minutes: int | None = None) -> TicketNode:
    return TicketNode(id=id, status=status, estimated_minutes=minutes)


# --------------------------------------------------------------------------- #
# progress_pct — half-up                                                       #
# --------------------------------------------------------------------------- #


def test_progress_half_up_rounds_toward_positive_infinity() -> None:
    assert progress_pct(2, 3) == 67  # 66.66.. -> 67, NOT 66 (banker's would give 67 here too)
    assert progress_pct(1, 8) == 13  # 12.5 -> 13 (half rounds up, not to even 12)
    assert progress_pct(0, 0) == 0  # total == 0 short-circuits
    assert progress_pct(0, 5) == 0
    assert progress_pct(5, 5) == 100
    assert progress_pct(1, 3) == 33  # 33.33.. -> 33


def test_progress_half_up_exact_half_cases() -> None:
    # 3/8 = 37.5 -> 38 ; 5/8 = 62.5 -> 63 ; 7/8 = 87.5 -> 88. Python round() would
    # bank these to 38/62/88 (even) — the floor(x+0.5) rule gives 38/63/88.
    assert progress_pct(3, 8) == 38
    assert progress_pct(5, 8) == 63
    assert progress_pct(7, 8) == 88


# --------------------------------------------------------------------------- #
# derive_epic_status                                                           #
# --------------------------------------------------------------------------- #


def test_derive_epic_status_no_tickets_is_todo() -> None:
    assert (
        derive_epic_status(
            ticket_count=0, completed_ticket_count=0, active_ticket_count=0, progress=0
        )
        == EpicStatus.TODO
    )


def test_derive_epic_status_full_progress_is_completed() -> None:
    assert (
        derive_epic_status(
            ticket_count=3, completed_ticket_count=3, active_ticket_count=0, progress=100
        )
        == EpicStatus.COMPLETED
    )


def test_derive_epic_status_active_or_completed_is_in_progress() -> None:
    assert (
        derive_epic_status(
            ticket_count=2, completed_ticket_count=0, active_ticket_count=1, progress=0
        )
        == EpicStatus.IN_PROGRESS
    )
    assert (
        derive_epic_status(
            ticket_count=4, completed_ticket_count=1, active_ticket_count=0, progress=25
        )
        == EpicStatus.IN_PROGRESS
    )


def test_derive_epic_status_only_pending_ready_is_todo() -> None:
    # Tickets exist but none active/completed and progress < 100 -> still todo.
    assert (
        derive_epic_status(
            ticket_count=2, completed_ticket_count=0, active_ticket_count=0, progress=0
        )
        == EpicStatus.TODO
    )


# --------------------------------------------------------------------------- #
# derive_epic_counts                                                           #
# --------------------------------------------------------------------------- #


def test_derive_epic_counts_buckets_and_minutes() -> None:
    tickets = [
        _ticket(TicketStatus.PENDING, id="a", minutes=10),
        _ticket(TicketStatus.READY, id="b", minutes=None),  # None -> 0
        _ticket(TicketStatus.ACTIVE, id="c", minutes=5),
        _ticket(TicketStatus.DONE, id="d", minutes=15),
        _ticket(TicketStatus.DONE, id="e", minutes=0),
    ]
    counts = derive_epic_counts(tickets)
    assert counts.ticket_count == 5
    assert counts.pending_ticket_count == 1
    assert counts.ready_ticket_count == 1
    assert counts.active_ticket_count == 1
    assert counts.completed_ticket_count == 2
    assert counts.estimated_minutes == 30
    assert counts.progress == progress_pct(2, 5) == 40
    assert counts.status == EpicStatus.IN_PROGRESS


def test_derive_epic_counts_empty_is_todo_zero() -> None:
    counts = derive_epic_counts([])
    assert counts == EpicCounts(
        ticket_count=0,
        pending_ticket_count=0,
        ready_ticket_count=0,
        active_ticket_count=0,
        completed_ticket_count=0,
        estimated_minutes=0,
        progress=0,
        status=EpicStatus.TODO,
    )


def test_derive_epic_counts_all_done_is_completed() -> None:
    counts = derive_epic_counts([_ticket(TicketStatus.DONE, id="a"), _ticket(TicketStatus.DONE, id="b")])
    assert counts.progress == 100
    assert counts.status == EpicStatus.COMPLETED


# --------------------------------------------------------------------------- #
# derive_spec_counts                                                           #
# --------------------------------------------------------------------------- #


def test_derive_spec_counts_epic_buckets_partition_epic_count() -> None:
    spec_epics = [
        EpicCountsView(status=EpicStatus.TODO),
        EpicCountsView(status=EpicStatus.TODO),
        EpicCountsView(status=EpicStatus.IN_PROGRESS),
        EpicCountsView(status=EpicStatus.COMPLETED),
    ]
    spec_tickets = [
        _ticket(TicketStatus.PENDING, id="a"),
        _ticket(TicketStatus.READY, id="b"),
        _ticket(TicketStatus.ACTIVE, id="c"),
        _ticket(TicketStatus.DONE, id="d", minutes=12),
    ]
    counts = derive_spec_counts(spec_epics, spec_tickets)
    assert counts.epic_count == 4
    assert counts.todo_epic_count == 2
    assert counts.in_progress_epic_count == 1
    assert counts.completed_epic_count == 1
    # epic buckets partition epic_count exactly.
    assert (
        counts.todo_epic_count + counts.in_progress_epic_count + counts.completed_epic_count
        == counts.epic_count
    )
    # ticket aggregates roll up directly from spec_tickets.
    assert counts.ticket_count == 4
    assert counts.pending_ticket_count == 1
    assert counts.ready_ticket_count == 1
    assert counts.active_ticket_count == 1
    assert counts.completed_ticket_count == 1
    assert counts.estimated_minutes == 12
    assert counts.progress == 25


def test_derive_spec_counts_empty() -> None:
    counts = derive_spec_counts([], [])
    assert counts.epic_count == 0
    assert counts.ticket_count == 0
    assert counts.progress == 0


def test_spec_view_from_counts_roundtrips_fields() -> None:
    counts = derive_spec_counts(
        [EpicCountsView(status=EpicStatus.COMPLETED)],
        [_ticket(TicketStatus.DONE, id="d")],
    )
    view = SpecCountsView.from_counts(SpecStatus.IN_PROGRESS, counts)
    assert view.status == SpecStatus.IN_PROGRESS
    assert view.epic_count == 1
    assert view.completed_epic_count == 1
    assert view.ticket_count == 1
    assert view.completed_ticket_count == 1


def test_epic_view_from_counts_carries_status() -> None:
    counts = derive_epic_counts([_ticket(TicketStatus.ACTIVE, id="a")])
    assert EpicCountsView.from_counts(counts) == EpicCountsView(status=EpicStatus.IN_PROGRESS)


# --------------------------------------------------------------------------- #
# derive_project_counts — bucket partition over all 8 spec statuses            #
# --------------------------------------------------------------------------- #


def _spec_view(status: SpecStatus) -> SpecCountsView:
    return SpecCountsView(
        status=status,
        epic_count=2,
        completed_epic_count=1,
        ticket_count=4,
        completed_ticket_count=2,
    )


def test_derive_project_counts_partitions_all_eight_statuses() -> None:
    # One spec per every one of the 8 SpecStatus values.
    specs = [_spec_view(status) for status in SpecStatus]
    counts = derive_project_counts(specs)

    assert counts.spec_count == 8
    assert counts.draft_spec_count == 1
    assert counts.planning_spec_count == 1
    assert counts.ready_spec_count == 1
    assert counts.in_progress_spec_count == 1
    # The three review statuses fold into ONE bucket.
    assert counts.in_review_spec_count == 3
    assert counts.completed_spec_count == 1

    # The six per-status buckets + completed partition spec_count exactly.
    bucket_sum = (
        counts.draft_spec_count
        + counts.planning_spec_count
        + counts.ready_spec_count
        + counts.in_progress_spec_count
        + counts.in_review_spec_count
        + counts.completed_spec_count
    )
    assert bucket_sum == counts.spec_count

    # Child rollups sum across all specs.
    assert counts.epic_count == 8 * 2
    assert counts.completed_epic_count == 8 * 1
    assert counts.ticket_count == 8 * 4
    assert counts.completed_ticket_count == 8 * 2
    # No progress attribute on the Project rollup.
    assert not hasattr(counts, "progress")


def test_derive_project_counts_empty() -> None:
    counts = derive_project_counts([])
    assert counts.spec_count == 0
    assert counts.in_review_spec_count == 0
    assert counts.ticket_count == 0


# --------------------------------------------------------------------------- #
# derive_dependency_counts                                                     #
# --------------------------------------------------------------------------- #


def test_dependency_counts_incoming_outgoing_and_spec_once() -> None:
    # a -> b, a -> c, d -> b  (X -> Y means "X depends on Y": X=ticket_id/from).
    edges = [
        DepEdge(ticket_id="a", depends_on_id="b"),
        DepEdge(ticket_id="a", depends_on_id="c"),
        DepEdge(ticket_id="d", depends_on_id="b"),
    ]
    ticket_epic: dict[str, str | None] = {"a": "e1", "b": "e1", "c": "e2", "d": "e2"}
    result = derive_dependency_counts(edges, ticket_epic)

    # outgoing on the dependent (ticket_id) end.
    assert result.ticket_outgoing == {"a": 2, "d": 1}
    # incoming on the blocker (depends_on_id) end.
    assert result.ticket_incoming == {"b": 2, "c": 1}
    # spec counts every intra-spec edge once.
    assert result.specification == 3


def test_dependency_counts_epic_only_when_both_endpoints_share_epic() -> None:
    edges = [
        DepEdge(ticket_id="a", depends_on_id="b"),  # both e1 -> counts for e1
        DepEdge(ticket_id="a", depends_on_id="c"),  # a=e1, c=e2 -> cross-epic, no epic count
        DepEdge(ticket_id="x", depends_on_id="y"),  # both e2 -> counts for e2
        DepEdge(ticket_id="p", depends_on_id="q"),  # p has no epic -> no epic count
    ]
    ticket_epic: dict[str, str | None] = {
        "a": "e1",
        "b": "e1",
        "c": "e2",
        "x": "e2",
        "y": "e2",
        "p": None,
        "q": "e2",
    }
    result = derive_dependency_counts(edges, ticket_epic)
    assert result.epic == {"e1": 1, "e2": 1}
    assert result.specification == 4


def test_dependency_counts_empty() -> None:
    result = derive_dependency_counts([], {})
    assert result.ticket_incoming == {}
    assert result.ticket_outgoing == {}
    assert result.specification == 0
    assert result.epic == {}


# --------------------------------------------------------------------------- #
# blueprint counts                                                            #
# --------------------------------------------------------------------------- #


def test_count_distinct_blueprint_ids() -> None:
    assert count_distinct_blueprint_ids([]) == 0
    assert count_distinct_blueprint_ids(["bp1", "bp1", "bp2"]) == 2


def test_blueprint_counts_epic_distinct_counts_shared_blueprint_once() -> None:
    # Two tickets in the SAME epic both reference blueprint bp1 -> epic counts bp1 once.
    refs = [
        BlueprintRef(ticket_id="t1", blueprint_id="bp1"),
        BlueprintRef(ticket_id="t2", blueprint_id="bp1"),
        BlueprintRef(ticket_id="t2", blueprint_id="bp2"),
        BlueprintRef(ticket_id="t3", blueprint_id="bp3"),  # different epic
    ]
    ticket_epic: dict[str, str | None] = {"t1": "e1", "t2": "e1", "t3": "e2"}
    spec_blueprint_ids = ["bp1", "bp2", "bp3"]
    result = derive_blueprint_counts(spec_blueprint_ids, refs, ticket_epic)

    # spec count = number of blueprint rows.
    assert result.specification == 3
    # per-ticket = number of reference rows for that ticket.
    assert result.ticket == {"t1": 1, "t2": 2, "t3": 1}
    # epic e1 has distinct {bp1, bp2} = 2 (bp1 shared by t1+t2 counts ONCE); e2 = {bp3}.
    assert result.epic == {"e1": 2, "e2": 1}


def test_blueprint_counts_ticket_without_epic_skips_epic_bucket() -> None:
    refs = [BlueprintRef(ticket_id="t1", blueprint_id="bp1")]
    result = derive_blueprint_counts(["bp1"], refs, {"t1": None})
    assert result.ticket == {"t1": 1}
    assert result.epic == {}


def test_blueprint_counts_empty() -> None:
    result = derive_blueprint_counts([], [], {})
    assert result.specification == 0
    assert result.ticket == {}
    assert result.epic == {}


# --------------------------------------------------------------------------- #
# change-detection gates                                                       #
# --------------------------------------------------------------------------- #


def test_has_epic_counts_changed_field_wise() -> None:
    base = derive_epic_counts([_ticket(TicketStatus.DONE, id="a")])
    same = derive_epic_counts([_ticket(TicketStatus.DONE, id="b")])  # ids irrelevant to counts
    changed = derive_epic_counts([_ticket(TicketStatus.PENDING, id="a")])
    assert has_epic_counts_changed(base, same) is False
    assert has_epic_counts_changed(base, changed) is True
