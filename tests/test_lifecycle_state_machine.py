"""Parity tests for the planning state machine (state-machine.ts / A1 §1.1)."""

from __future__ import annotations

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.state_machine import (
    PHASE_ORDER,
    is_later_phase,
    is_terminal_phase,
    next_phase,
    phase_index,
)


def test_phase_order_is_the_seven_phases_in_order() -> None:
    assert [p.value for p in PHASE_ORDER] == [
        "planning_spec",
        "epic_decomposition",
        "epic_expansion",
        "ticket_decomposition",
        "ticket_expansion",
        "cross_validation",
        "planned",
    ]


def test_phase_order_covers_every_planning_phase_member() -> None:
    assert set(PHASE_ORDER) == set(PlanningPhase)
    assert len(PHASE_ORDER) == len(set(PHASE_ORDER)) == 7


def test_next_phase_walks_the_full_chain() -> None:
    assert next_phase(PlanningPhase.PLANNING_SPEC) == PlanningPhase.EPIC_DECOMPOSITION
    assert next_phase(PlanningPhase.EPIC_DECOMPOSITION) == PlanningPhase.EPIC_EXPANSION
    assert next_phase(PlanningPhase.EPIC_EXPANSION) == PlanningPhase.TICKET_DECOMPOSITION
    assert next_phase(PlanningPhase.TICKET_DECOMPOSITION) == PlanningPhase.TICKET_EXPANSION
    assert next_phase(PlanningPhase.TICKET_EXPANSION) == PlanningPhase.CROSS_VALIDATION


def test_next_phase_cross_validation_yields_planned() -> None:
    assert next_phase(PlanningPhase.CROSS_VALIDATION) == PlanningPhase.PLANNED


def test_next_phase_planned_is_none() -> None:
    assert next_phase(PlanningPhase.PLANNED) is None


def test_next_phase_chain_terminates_after_seven_steps() -> None:
    phase: PlanningPhase | None = PHASE_ORDER[0]
    visited: list[PlanningPhase] = []
    while phase is not None:
        visited.append(phase)
        phase = next_phase(phase)
    assert visited == list(PHASE_ORDER)


def test_is_terminal_phase_only_cross_validation() -> None:
    assert is_terminal_phase(PlanningPhase.CROSS_VALIDATION) is True
    for phase in PHASE_ORDER:
        if phase is PlanningPhase.CROSS_VALIDATION:
            continue
        assert is_terminal_phase(phase) is False


def test_is_terminal_phase_excludes_the_planned_sentinel() -> None:
    assert is_terminal_phase(PlanningPhase.PLANNED) is False


def test_phase_index_matches_position() -> None:
    assert [phase_index(p) for p in PHASE_ORDER] == list(range(7))
    assert phase_index(PlanningPhase.PLANNING_SPEC) == 0
    assert phase_index(PlanningPhase.PLANNED) == 6


def test_is_later_phase_strict_ordering() -> None:
    assert is_later_phase(PlanningPhase.PLANNED, PlanningPhase.PLANNING_SPEC) is True
    assert is_later_phase(PlanningPhase.CROSS_VALIDATION, PlanningPhase.EPIC_EXPANSION) is True
    assert is_later_phase(PlanningPhase.EPIC_DECOMPOSITION, PlanningPhase.TICKET_EXPANSION) is False
    # Not strictly later than itself.
    assert is_later_phase(PlanningPhase.EPIC_EXPANSION, PlanningPhase.EPIC_EXPANSION) is False


def test_is_later_phase_consistent_with_phase_index_for_all_pairs() -> None:
    for a in PHASE_ORDER:
        for b in PHASE_ORDER:
            assert is_later_phase(a, b) == (phase_index(a) > phase_index(b))
