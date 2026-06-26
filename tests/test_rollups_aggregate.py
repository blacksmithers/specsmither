"""Tests for the planning aggregate + datapoint folds (rollups/aggregate.py).

Acceptance criteria:
* datapoint id idempotency — re-deriving the same ``(trigger_action_id,
  entity_id, trigger)`` triple yields an identical id (the upsert key);
* the replay invariant — ``replay_compute`` over the whole stream equals folding
  ``compute_aggregate_update`` over the same stream item-by-item, and equals a
  batched replay;
* projection-rollup spot checks — ``trigger_breakdown`` counts and
  ``current_scores.per_entity`` last-wins.
"""

from __future__ import annotations

from collections.abc import Callable

from specsmither.rollups.aggregate import (
    Action,
    Aggregate,
    Datapoint,
    SessionContext,
    Transition,
    compute_aggregate_update,
    compute_datapoints,
    create_empty_aggregate,
    datapoint_id,
    replay_compute,
)

CTX = SessionContext(planning_session_id="session-1", project_id="project-1", specification_id="spec-1")

#: A fixed clock so ``last_updated_at`` is identical across every fold path —
#: replay (one ``now()`` call) must equal the incremental fold (many calls).
NOW: Callable[[], str] = lambda: "2026-06-26T00:00:00+00:00"  # noqa: E731


def _ts(seconds: int) -> str:
    return f"2026-01-01T00:00:{seconds:02d}+00:00"


def _sample_actions() -> list[Action]:
    return [
        Action(
            id="A-001", planning_session_id="session-1", performed_at=_ts(1),
            phase="epic_decomposition", operation="create_epic", entity_id="epic-1",
            payload={"title": "Epic A"}, score_after=10.0,
        ),
        Action(
            id="A-002", planning_session_id="session-1", performed_at=_ts(2),
            phase="ticket_decomposition", operation="create_ticket", entity_id="ticket-1",
            payload={"title": "Ticket A"}, score_after=20.0,
        ),
        Action(
            id="A-003", planning_session_id="session-1", performed_at=_ts(3),
            phase="ticket_decomposition", operation="create_ticket", entity_id="ticket-2",
            payload={"title": "Ticket B"}, score_after=22.0,
        ),
        Action(
            id="A-004", planning_session_id="session-1", performed_at=_ts(4),
            phase="epic_expansion", operation="update_epic", entity_id="epic-1",
            payload={"fields": {"title": "Epic A v2"}}, score_after=30.0,
            per_entity_scores_after={"epic-1": 30.0},
        ),
        Action(
            id="A-005", planning_session_id="session-1", performed_at=_ts(5),
            phase="cross_validation", operation="create_dependencies",
            payload={"dependencies": [{"a": 1}, {"a": 2}]}, score_after=31.0,
            per_entity_scores_after={"ticket-1": 0.5, "ticket-2": 0.6},
        ),
        Action(
            id="A-006", planning_session_id="session-1", performed_at=_ts(6),
            phase="epic_decomposition", operation="create_epic", entity_id="epic-2",
            outcome="denied", deny_reason="spec_frozen", score_after=99.0,
        ),
        Action(
            id="A-007", planning_session_id="session-1", performed_at=_ts(7),
            phase="ticket_expansion", operation="update_ticket", entity_id="ticket-1",
            score_after=32.0, per_entity_scores_after={"ticket-1": 0.8},
        ),
    ]


def _sample_transitions() -> list[Transition]:
    return [
        Transition(
            id="T-001", planning_session_id="session-1", triggered_at=_ts(0),
            from_phase="planning_spec", to_phase="planning_spec", trigger="auto_initial",
        ),
        Transition(
            id="T-002", planning_session_id="session-1", triggered_at=_ts(2),
            from_phase="planning_spec", to_phase="epic_decomposition", trigger="ai_agent",
        ),
        Transition(
            id="T-003", planning_session_id="session-1", triggered_at=_ts(4),
            from_phase="epic_decomposition", to_phase="epic_expansion", trigger="ai_agent",
        ),
    ]


# ---------------------------------------------------------------------------
# Datapoint id idempotency (the acceptance criterion)
# ---------------------------------------------------------------------------


def test_datapoint_id_is_the_deterministic_composite() -> None:
    assert datapoint_id("A-001", "epic-1", "created") == "A-001#epic-1#created"


def test_compute_datapoints_ids_are_idempotent_across_re_derivation() -> None:
    action = Action(
        id="A-001", planning_session_id="session-1", performed_at=_ts(1),
        phase="epic_decomposition", operation="create_epic", entity_id="epic-1",
        score_after=10.0,
    )
    first = compute_datapoints(
        [action], project_id="project-1", specification_id="spec-1", entity_registry={}
    )
    second = compute_datapoints(
        [action], project_id="project-1", specification_id="spec-1", entity_registry={}
    )
    # Same (action, entity, trigger) -> identical id (upserts, never duplicates).
    assert [d.id for d in first] == ["A-001#epic-1#created"]
    assert [d.id for d in first] == [d.id for d in second]
    assert first == second


def test_denied_actions_emit_no_datapoints() -> None:
    denied = Action(
        id="A-001", planning_session_id="session-1", performed_at=_ts(1),
        phase="epic_decomposition", operation="create_epic", entity_id="epic-1",
        outcome="denied", score_after=99.0,
    )
    assert compute_datapoints(
        [denied], project_id="project-1", specification_id="spec-1", entity_registry={}
    ) == []


# ---------------------------------------------------------------------------
# Replay invariant
# ---------------------------------------------------------------------------


def _fold_incrementally(
    actions: list[Action], transitions: list[Transition]
) -> tuple[Aggregate, list[Datapoint]]:
    """Fold one item at a time (transitions first, then actions), preserving
    intra-kind order — the chunking the replay invariant must be associative
    over."""
    agg: Aggregate | None = None
    datapoints: list[Datapoint] = []
    for t in transitions:
        result = compute_aggregate_update(agg, (), [t], session_context=CTX, now=NOW)
        agg = result.aggregate
    for a in actions:
        result = compute_aggregate_update(agg, [a], (), session_context=CTX, now=NOW)
        agg = result.aggregate
        datapoints.extend(result.datapoints)
    assert agg is not None
    return agg, datapoints


def test_replay_equals_incremental_fold() -> None:
    actions = _sample_actions()
    transitions = _sample_transitions()

    replay = replay_compute(actions, transitions, session_context=CTX, now=NOW)
    folded_agg, folded_datapoints = _fold_incrementally(actions, transitions)

    assert folded_agg == replay.aggregate
    assert folded_datapoints == replay.datapoints

    # A few spot checks on the replayed projection for clarity.
    agg = replay.aggregate
    # opsMix counts denied actions too (A-001 success + A-006 denied create_epic).
    assert agg.ops_mix == {
        "create_epic": 2, "create_ticket": 2, "update_epic": 1,
        "create_dependencies": 1, "update_ticket": 1,
    }
    assert agg.entity_counts == {"epic": 1, "ticket": 2, "blueprint": 0, "dependency": 2}
    assert agg.denied_by_reason == {"spec_frozen": 1}
    assert agg.current_scores["global"] == 32.0
    assert len(agg.score_history_global) == 6  # the 6 scored success actions
    assert agg.last_processed_action_id == "A-007"
    assert agg.last_processed_transition_id == "T-003"


def test_batched_replay_equals_single_pass() -> None:
    actions = _sample_actions()
    transitions = _sample_transitions()

    single = replay_compute(actions, transitions, session_context=CTX, now=NOW)
    batched = replay_compute(actions, transitions, session_context=CTX, now=NOW, batch_size=2)

    assert batched.aggregate == single.aggregate
    assert batched.datapoints == single.datapoints
    assert batched.batches > 1


# ---------------------------------------------------------------------------
# Projection-rollup spot checks
# ---------------------------------------------------------------------------


def test_trigger_breakdown_counts_every_transition_trigger() -> None:
    result = compute_aggregate_update(
        None, (), _sample_transitions(), session_context=CTX, now=NOW
    )
    assert result.aggregate.trigger_breakdown == {"auto_initial": 1, "ai_agent": 2}


def test_phase_timeline_closes_entries_with_duration() -> None:
    result = compute_aggregate_update(
        None, (), _sample_transitions(), session_context=CTX, now=NOW
    )
    timeline = result.aggregate.phase_timeline
    assert [e["phase"] for e in timeline] == [
        "planning_spec", "epic_decomposition", "epic_expansion",
    ]
    assert timeline[0]["duration_ms"] == 2000  # _ts(0) -> _ts(2)
    assert timeline[1]["duration_ms"] == 2000  # _ts(2) -> _ts(4)
    assert "exited_at" not in timeline[2]  # last entry stays open


def test_current_scores_per_entity_is_last_wins() -> None:
    replay = replay_compute(_sample_actions(), session_context=CTX, now=NOW)
    per_entity = replay.aggregate.current_scores["per_entity"]
    # ticket-1: created(20) -> dependency_added(0.5) -> ticket_field_updated(0.8).
    assert per_entity["ticket-1"] == {"type": "ticket", "score": 0.8}
    assert per_entity["ticket-2"] == {"type": "ticket", "score": 0.6}
    assert per_entity["epic-1"] == {"type": "epic", "score": 30.0}


def test_score_delta_by_operation_skips_denied_and_first_score() -> None:
    replay = replay_compute(_sample_actions(), session_context=CTX, now=NOW)
    deltas = replay.aggregate.score_delta_by_operation
    # A-001(10) has no predecessor -> no delta. Denied A-006 never updates prev.
    assert deltas["create_ticket"] == {"sum": 12.0, "count": 2}  # +10, +2
    assert deltas["update_epic"] == {"sum": 8.0, "count": 1}  # 30-22
    assert deltas["create_dependencies"] == {"sum": 1.0, "count": 1}  # 31-30
    assert deltas["update_ticket"] == {"sum": 1.0, "count": 1}  # 32-31 (skips denied 99)


def test_denied_action_excluded_from_counts_revisions_scores() -> None:
    denied = Action(
        id="A-001", planning_session_id="session-1", performed_at=_ts(1),
        phase="epic_decomposition", operation="create_epic", entity_id="epic-1",
        outcome="denied", deny_reason="spec_frozen", score_after=99.0,
    )
    result = compute_aggregate_update(None, [denied], (), session_context=CTX, now=NOW)
    agg = result.aggregate
    assert agg.ops_mix == {"create_epic": 1}
    assert agg.entity_counts["epic"] == 0
    assert "epic-1" not in agg.entity_revisions
    assert agg.score_history_global == []
    assert "global" not in agg.current_scores
    assert agg.denied_by_reason == {"spec_frozen": 1}
    assert result.datapoints == []


def test_empty_seed_round_trips() -> None:
    seed = create_empty_aggregate(CTX)
    assert seed.planning_session_id == "session-1"
    assert seed.entity_counts == {"epic": 0, "ticket": 0, "blueprint": 0, "dependency": 0}
    assert seed.phase_timeline == []

    result = compute_aggregate_update(None, (), (), session_context=CTX, now=NOW)
    assert result.aggregate.last_processed_action_id is None
    assert result.aggregate.last_processed_transition_id is None
    assert result.aggregate.last_updated_at == "2026-06-26T00:00:00+00:00"
    assert result.datapoints == []
