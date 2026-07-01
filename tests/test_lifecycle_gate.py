"""Parity tests for the phase gate evaluator (phase-gate-evaluator.ts / A1 §1.6).

The gate is pure and thin: it trusts ``validator_output.gate_result`` for the
binary phases (``planning_spec`` / ``*_decomposition`` / ``cross_validation``)
and does a per-entity threshold compare for the two ``*_expansion`` phases.
These tests pin that switch, the empty-touched-set guard, the per-phase
score-write triggers, and the ``cross_validation`` full refresh.
"""

from __future__ import annotations

from specsmither.domain.enums import DatapointTrigger, FindingCategory, PlanningPhase
from specsmither.lifecycle.gate import (
    EntityScoreWrite,
    PhaseGateResult,
    evaluate_phase_gate,
)
from specsmither.lifecycle.ports import ValidatorFinding, ValidatorOutput

THRESHOLDS = {"specification": 0.8, "epic": 0.8, "ticket": 0.8}


def _output(
    *,
    gate_result: str = "pass",
    local_score: float = 1.0,
    per_epic_score: dict[str, float] | None = None,
    per_ticket_score: dict[str, float] | None = None,
    findings: list[ValidatorFinding] | None = None,
    validated_phase: PlanningPhase = PlanningPhase.PLANNING_SPEC,
) -> ValidatorOutput:
    return ValidatorOutput(
        gate_result=gate_result,  # type: ignore[arg-type]
        local_score=local_score,
        per_epic_score=per_epic_score or {},
        per_ticket_score=per_ticket_score or {},
        findings=findings or [],
        validated_phase=validated_phase,
    )


# --------------------------------------------------------------------------- #
# planning_spec — trusts gate_result; one spec score write                    #
# --------------------------------------------------------------------------- #


def test_planning_spec_trusts_validator_gate_result_pass() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.PLANNING_SPEC,
        validator_output=_output(gate_result="pass", local_score=0.91),
        touched_entity_ids={"spec-1"},
        thresholds=THRESHOLDS,
    )
    assert isinstance(result, PhaseGateResult)
    assert result.gate_outcome == "pass"
    assert result.entity_verdicts == []
    assert result.entity_score_writes == [
        EntityScoreWrite(
            entity_type="spec",
            entity_id="spec-1",
            score=0.91,
            trigger=DatapointTrigger.METADATA_UPDATED,
        )
    ]


def test_planning_spec_fail_mirrors_validator_even_with_high_score() -> None:
    # The validator denies (e.g. cascade failure) despite a score above threshold;
    # the gate trusts gate_result, not the raw score.
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.PLANNING_SPEC,
        validator_output=_output(gate_result="fail", local_score=0.99),
        touched_entity_ids=["spec-1"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    assert result.entity_score_writes[0].score == 0.99
    assert result.entity_score_writes[0].trigger == DatapointTrigger.METADATA_UPDATED


def test_planning_spec_empty_touched_emits_no_write() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.PLANNING_SPEC,
        validator_output=_output(gate_result="pass"),
        touched_entity_ids=set(),
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "pass"
    assert result.entity_score_writes == []


# --------------------------------------------------------------------------- #
# decomposition phases — binary, no writes                                    #
# --------------------------------------------------------------------------- #


def test_epic_decomposition_is_binary_with_no_writes() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_DECOMPOSITION,
        validator_output=_output(gate_result="fail"),
        touched_entity_ids={"epic-1"},
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    assert result.entity_verdicts == []
    assert result.entity_score_writes == []
    assert "epic_decomposition" in result.rationale


def test_ticket_decomposition_is_binary_with_no_writes() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.TICKET_DECOMPOSITION,
        validator_output=_output(gate_result="pass"),
        touched_entity_ids={"tk-1"},
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "pass"
    assert result.entity_score_writes == []
    assert "ticket_decomposition" in result.rationale


# --------------------------------------------------------------------------- #
# epic_expansion — all touched epics must clear the epic threshold            #
# --------------------------------------------------------------------------- #


def test_epic_expansion_passes_when_all_touched_epics_meet_threshold() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e1": 0.8, "e2": 0.95}),
        touched_entity_ids=["e1", "e2"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "pass"
    assert [(v.entity_id, v.verdict) for v in result.entity_verdicts] == [
        ("e1", "pass"),
        ("e2", "pass"),
    ]
    assert all(v.entity_type == "epic" for v in result.entity_verdicts)
    assert result.entity_score_writes == [
        EntityScoreWrite("epic", "e1", 0.8, DatapointTrigger.EPIC_FIELD_UPDATED),
        EntityScoreWrite("epic", "e2", 0.95, DatapointTrigger.EPIC_FIELD_UPDATED),
    ]


def test_epic_expansion_fails_when_one_touched_epic_below_threshold() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e1": 0.95, "e2": 0.5}),
        touched_entity_ids=["e1", "e2"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    verdicts = {v.entity_id: v.verdict for v in result.entity_verdicts}
    assert verdicts == {"e1": "pass", "e2": "review_needed"}
    # Score writes are emitted for ALL touched epics, pass or fail.
    assert len(result.entity_score_writes) == 2


def test_epic_expansion_missing_score_defaults_to_zero_and_fails() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={}),
        touched_entity_ids=["e1"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    assert result.entity_verdicts[0].score == 0.0
    assert result.entity_verdicts[0].verdict == "review_needed"


def test_epic_expansion_empty_touched_set_fails() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e1": 0.95}),
        touched_entity_ids=set(),
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    assert result.entity_verdicts == []
    assert result.entity_score_writes == []
    assert "no touched epics" in result.rationale


def test_epic_expansion_review_hints_grouped_from_findings() -> None:
    findings = [
        ValidatorFinding(
            category=FindingCategory.RUBRIC,
            message="Objective too vague.",
            severity="finding",
            entity_id="e2",
        ),
        ValidatorFinding(
            category=FindingCategory.RUBRIC,
            message="Add success metric.",
            severity="finding",
            entity_id="e2",
        ),
        ValidatorFinding(
            category=FindingCategory.RUBRIC,
            message="Unrelated to e2.",
            severity="finding",
            entity_id="e1",
        ),
    ]
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e2": 0.4}, findings=findings),
        touched_entity_ids=["e2"],
        thresholds=THRESHOLDS,
    )
    assert result.entity_verdicts[0].review_hints == [
        "Objective too vague.",
        "Add success metric.",
    ]


# --------------------------------------------------------------------------- #
# ticket_expansion — symmetric over tickets                                   #
# --------------------------------------------------------------------------- #


def test_ticket_expansion_passes_when_all_touched_tickets_meet_threshold() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.TICKET_EXPANSION,
        validator_output=_output(per_ticket_score={"t1": 0.9, "t2": 0.81}),
        touched_entity_ids=["t1", "t2"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "pass"
    assert all(v.entity_type == "ticket" for v in result.entity_verdicts)
    assert result.entity_score_writes == [
        EntityScoreWrite("ticket", "t1", 0.9, DatapointTrigger.TICKET_FIELD_UPDATED),
        EntityScoreWrite("ticket", "t2", 0.81, DatapointTrigger.TICKET_FIELD_UPDATED),
    ]


def test_ticket_expansion_fails_when_one_touched_ticket_below_threshold() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.TICKET_EXPANSION,
        validator_output=_output(per_ticket_score={"t1": 0.9, "t2": 0.3}),
        touched_entity_ids=["t1", "t2"],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    verdicts = {v.entity_id: v.verdict for v in result.entity_verdicts}
    assert verdicts == {"t1": "pass", "t2": "review_needed"}


def test_ticket_expansion_empty_touched_set_fails() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.TICKET_EXPANSION,
        validator_output=_output(per_ticket_score={"t1": 0.9}),
        touched_entity_ids=[],
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "fail"
    assert result.entity_score_writes == []
    assert "no touched tickets" in result.rationale


# --------------------------------------------------------------------------- #
# cross_validation — binary gate + full refresh of all entities               #
# --------------------------------------------------------------------------- #


def test_cross_validation_emits_full_refresh_for_spec_all_epics_all_tickets() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.CROSS_VALIDATION,
        validator_output=_output(
            gate_result="pass",
            local_score=0.88,
            per_epic_score={"e1": 0.9, "e2": 0.7},
            per_ticket_score={"t1": 0.95, "t2": 0.6},
        ),
        touched_entity_ids={"spec-1"},
        thresholds=THRESHOLDS,
        all_epic_ids=["e1", "e2"],
        all_ticket_ids=["t1", "t2"],
    )
    assert result.gate_outcome == "pass"
    assert result.entity_verdicts == []
    assert result.entity_score_writes == [
        EntityScoreWrite("spec", "spec-1", 0.88, DatapointTrigger.CROSS_VAL_RECOMPUTE),
        EntityScoreWrite("epic", "e1", 0.9, DatapointTrigger.CROSS_VAL_RECOMPUTE),
        EntityScoreWrite("epic", "e2", 0.7, DatapointTrigger.CROSS_VAL_RECOMPUTE),
        EntityScoreWrite("ticket", "t1", 0.95, DatapointTrigger.CROSS_VAL_RECOMPUTE),
        EntityScoreWrite("ticket", "t2", 0.6, DatapointTrigger.CROSS_VAL_RECOMPUTE),
    ]
    assert all(
        w.trigger == DatapointTrigger.CROSS_VAL_RECOMPUTE for w in result.entity_score_writes
    )


def test_cross_validation_gate_outcome_is_binary_from_validator() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.CROSS_VALIDATION,
        validator_output=_output(gate_result="fail", local_score=0.5),
        touched_entity_ids={"spec-1"},
        thresholds=THRESHOLDS,
        all_epic_ids=[],
        all_ticket_ids=[],
    )
    assert result.gate_outcome == "fail"
    # Only the spec write survives when there are no epics/tickets.
    assert result.entity_score_writes == [
        EntityScoreWrite("spec", "spec-1", 0.5, DatapointTrigger.CROSS_VAL_RECOMPUTE),
    ]


def test_cross_validation_without_spec_id_skips_spec_write() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.CROSS_VALIDATION,
        validator_output=_output(gate_result="pass", per_epic_score={"e1": 0.9}),
        touched_entity_ids=set(),
        thresholds=THRESHOLDS,
        all_epic_ids=["e1"],
        all_ticket_ids=[],
    )
    assert result.entity_score_writes == [
        EntityScoreWrite("epic", "e1", 0.9, DatapointTrigger.CROSS_VAL_RECOMPUTE),
    ]


# --------------------------------------------------------------------------- #
# planned — trivial pass                                                       #
# --------------------------------------------------------------------------- #


def test_planned_is_a_trivial_pass_with_no_writes() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.PLANNED,
        validator_output=_output(gate_result="fail"),
        touched_entity_ids=set(),
        thresholds=THRESHOLDS,
    )
    assert result.gate_outcome == "pass"
    assert result.entity_verdicts == []
    assert result.entity_score_writes == []


# --------------------------------------------------------------------------- #
# determinism — a set input yields a stable (sorted) write order              #
# --------------------------------------------------------------------------- #


def test_set_touched_ids_yield_deterministic_sorted_order() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e1": 0.9, "e2": 0.9, "e3": 0.9}),
        touched_entity_ids={"e3", "e1", "e2"},
        thresholds=THRESHOLDS,
    )
    assert [w.entity_id for w in result.entity_score_writes] == ["e1", "e2", "e3"]


def test_list_touched_ids_preserve_caller_order() -> None:
    result = evaluate_phase_gate(
        current_phase=PlanningPhase.EPIC_EXPANSION,
        validator_output=_output(per_epic_score={"e1": 0.9, "e2": 0.9, "e3": 0.9}),
        touched_entity_ids=["e3", "e1", "e2"],
        thresholds=THRESHOLDS,
    )
    assert [w.entity_id for w in result.entity_score_writes] == ["e3", "e1", "e2"]
