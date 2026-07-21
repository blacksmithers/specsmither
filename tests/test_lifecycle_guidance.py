"""Parity tests for the minimal guidance composer (work item #11).

Covers the two public entry points the L4 verbs call:

* :func:`compose_response` — the variant-keyed umbrella. Pins that ``gate_passed`` /
  ``gate_failed`` / ``denied`` each produce non-empty English prose with the right
  ``outcome`` / ``gate_result``, that ``next_entities`` is capped by the configured
  ``maxNextEntitiesToShow`` (and the built-in default of 3), and that findings drive the
  recommended-moves block.
* :func:`compose_get_planning_status` — pins the deterministic variant precedence
  (closed / awaiting / active+feedback / active+unread-transition / active+plain) and the
  one-shot ``clear_pending_feedback`` / ``bump_last_read_at`` flags.
"""

from __future__ import annotations

from typing import Any

from specsmither.db.models import PlanningSession
from specsmither.domain.enums import GuidanceVariant, PlanningPhase, PlanningSessionStatus
from specsmither.lifecycle.gate import EntityVerdict, PhaseGateResult
from specsmither.lifecycle.guidance.compose import (
    compose_get_planning_status,
    compose_response,
    pick_get_planning_status_variant,
)
from specsmither.lifecycle.guidance.types import PlanningAgentResponse
from specsmither.lifecycle.ports import ValidatorFinding, ValidatorOutput
from specsmither.lifecycle.prechecks import Denied

VALIDATOR_CONFIG: dict[str, Any] = {
    "thresholds": {"specification": 80, "epic": 75, "ticket": 70},
}


def _session(**overrides: Any) -> PlanningSession:
    """A bare in-memory planning session with sensible defaults for the composer."""

    defaults: dict[str, Any] = {
        "id": "ps-1",
        "specification_id": "spec-1",
        "status": PlanningSessionStatus.ACTIVE.value,
        "current_phase": PlanningPhase.EPIC_EXPANSION.value,
        "actions_count": 4,
        "last_score": 0.9,
        "last_gate_result": "pass",
    }
    defaults.update(overrides)
    return PlanningSession(**defaults)


def _output(gate: str, findings: list[ValidatorFinding]) -> ValidatorOutput:
    return ValidatorOutput(
        gate_result=gate,  # type: ignore[arg-type]
        local_score=0.65 if gate == "fail" else 0.92,
        per_epic_score={},
        per_ticket_score={},
        findings=findings,
        validated_phase=PlanningPhase.EPIC_EXPANSION,
    )


def _verdicts(n: int) -> list[EntityVerdict]:
    return [
        EntityVerdict(
            entity_id=f"e{i}",
            entity_type="epic",
            score=0.5,
            verdict="review_needed",
            review_hints=[f"epic e{i} is too thin"],
        )
        for i in range(n)
    ]


# --------------------------------------------------------------------------- #
# compose_response — gate_passed / gate_failed / denied                        #
# --------------------------------------------------------------------------- #


def test_compose_response_gate_passed() -> None:
    resp = compose_response(
        variant=GuidanceVariant.GATE_PASSED,
        session=_session(),
        validator_output=_output("pass", []),
        validator_config=VALIDATOR_CONFIG,
    )
    assert isinstance(resp, PlanningAgentResponse)
    assert resp.outcome == "success"
    assert resp.gate_result == "pass"
    assert resp.gate_passed is True
    assert resp.variant == GuidanceVariant.GATE_PASSED
    assert resp.response_kind == "planning_agent_response"
    assert len(resp.guidance) > 20
    assert "complete_planning_session" in resp.guidance


def test_compose_response_gate_failed_has_findings_and_moves() -> None:
    findings = [
        ValidatorFinding(
            category="rubric",
            message="Spec goals are underspecified.",
            severity="finding",
            path="/spec/goals",
            global_impact_on_fix=0.4,
        ),
    ]
    resp = compose_response(
        variant=GuidanceVariant.GATE_FAILED,
        session=_session(last_gate_result="fail"),
        validator_output=_output("fail", findings),
        validator_config=VALIDATOR_CONFIG,
    )
    assert resp.outcome == "success"
    assert resp.gate_result == "fail"
    assert resp.gate_passed is False
    assert len(resp.guidance) > 20
    assert resp.findings_summary is not None
    assert len(resp.findings) == 1
    # The /spec/goals finding maps to the update_spec fix verb.
    assert [m.operation for m in resp.recommended_moves] == ["update_spec"]
    assert resp.recommended_moves[0].rationale == "Spec goals are underspecified."


def test_compose_response_denied_carries_message_and_blockers() -> None:
    denial = Denied(
        code="gate_not_passed",
        message="The phase gate is not passing.",
        blockers=["epic e1 too thin", "epic e2 missing tickets"],
    )
    resp = compose_response(
        variant=GuidanceVariant.DENIED,
        session=_session(),
        denial=denial,
        validator_config=VALIDATOR_CONFIG,
    )
    assert resp.outcome == "denied"
    assert "The phase gate is not passing." in resp.guidance
    assert "epic e1 too thin" in resp.guidance


# --------------------------------------------------------------------------- #
# compose_response — next_entities capping                                     #
# --------------------------------------------------------------------------- #


def test_next_entities_capped_by_config() -> None:
    gate = PhaseGateResult(gate_outcome="pass", entity_verdicts=_verdicts(5))
    resp = compose_response(
        variant=GuidanceVariant.GATE_PASSED,
        session=_session(),
        validator_output=_output("pass", []),
        gate_result=gate,
        lifecycle_config={"guidance": {"maxNextEntitiesToShow": 2}},
        validator_config=VALIDATOR_CONFIG,
    )
    assert len(resp.next_entities) == 2
    assert resp.next_entities[0].entity_id == "e0"
    assert resp.next_entities[0].hint == "epic e0 is too thin"


def test_next_entities_default_cap_is_three() -> None:
    gate = PhaseGateResult(gate_outcome="pass", entity_verdicts=_verdicts(5))
    resp = compose_response(
        variant=GuidanceVariant.GATE_PASSED,
        session=_session(),
        validator_output=_output("pass", []),
        gate_result=gate,
        validator_config=VALIDATOR_CONFIG,
    )
    assert len(resp.next_entities) == 3


# --------------------------------------------------------------------------- #
# compose_get_planning_status — variant precedence + one-shot flags            #
# --------------------------------------------------------------------------- #


def test_gps_closed_session() -> None:
    session = _session(status=PlanningSessionStatus.CLOSED.value)
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.SESSION_CLOSED
    assert comp.clear_pending_feedback is False
    assert comp.bump_last_read_at is False
    assert comp.response.outcome == "success"
    assert len(comp.response.guidance) > 20


def test_gps_awaiting_human_review() -> None:
    session = _session(status=PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value)
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.AWAITING_HUMAN_REVIEW_HANDOVER
    assert comp.clear_pending_feedback is False


def test_gps_active_with_pending_feedback_clears_it() -> None:
    session = _session(
        pending_human_feedback={"content": "Tighten the acceptance criteria.", "recordedAt": "x"},
    )
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.HUMAN_FEEDBACK_RECEIVED
    assert comp.clear_pending_feedback is True
    assert comp.bump_last_read_at is False
    assert "Tighten the acceptance criteria." in comp.response.guidance
    # The poll seeds a synthetic "apply the feedback" move (the phase's first native op),
    # since it carries no validator findings of its own.
    moves = comp.response.recommended_moves
    assert [m.operation for m in moves] == ["update_epic"]  # epic_expansion default
    assert moves[0].rationale == "Apply the human's feedback to the relevant entity."


def test_gps_active_unread_approve_transition_bumps_read() -> None:
    session = _session(
        last_transition_at="2026-06-26T10:00:00Z",
        last_read_at="2026-06-26T09:00:00Z",
        last_transition_trigger="human_approve",
    )
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.PHASE_ADVANCED_AFTER_APPROVE
    assert comp.bump_last_read_at is True
    assert comp.clear_pending_feedback is False


def test_gps_active_unread_reject_no_feedback_transition() -> None:
    session = _session(
        last_transition_at="2026-06-26T10:00:00Z",
        last_read_at=None,
        last_transition_trigger="human_reject_no_feedback",
    )
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK
    assert comp.bump_last_read_at is True


def test_gps_active_plain_status_report() -> None:
    session = _session(
        last_transition_at="2026-06-26T09:00:00Z",
        last_read_at="2026-06-26T10:00:00Z",
        last_transition_trigger="ai_agent",
    )
    comp = compose_get_planning_status(session, validator_config=VALIDATOR_CONFIG)
    assert comp.variant == GuidanceVariant.PHASE_STATUS_REPORT
    assert comp.clear_pending_feedback is False
    assert comp.bump_last_read_at is False
    # The plain report shows the cached gate + score.
    assert comp.response.gate_result == "pass"
    assert comp.response.score == 0.9


def test_pick_variant_is_pure_function_of_session() -> None:
    assert pick_get_planning_status_variant(_session()) == GuidanceVariant.PHASE_STATUS_REPORT


# --------------------------------------------------------------------------- #
# Status report reflects the PHASE-GATE verdict, not the composite gate_result #
# (simulator judge find 2026-07-13: "gate fail, score 100 / threshold 70").    #
# --------------------------------------------------------------------------- #


def _spec_full_with_tickets(*ticket_ids: str) -> Any:
    from specsmither.lifecycle.ports import EpicFull, SpecFull, TicketRef

    tickets = [TicketRef(id=tid, epic_id="e1", title=tid) for tid in ticket_ids]
    epic = EpicFull(id="e1", specification_id="spec-1", title="E1", tickets=tickets, description="d")
    return SpecFull(
        spec=type("S", (), {"id": "spec-1", "status": "planning"})(),
        epics=[epic],
        blueprints=[],
    )


def test_status_report_shows_phase_gate_pass_despite_cascade_fail() -> None:
    # At ticket_expansion, every ticket clears the threshold (100 ≥ 70) so the PHASE gate
    # passes — but the validator's composite gate_result is 'fail' (the global cascade:
    # the DAG is not wired yet). The status report must show the phase-gate verdict, not
    # the composite, so it never says the self-contradictory "gate fail, score 100".
    output = ValidatorOutput(
        gate_result="fail",  # composite folds in the failing cascade
        local_score=100.0,
        per_epic_score={},
        per_ticket_score={"t1": 100.0, "t2": 100.0},  # every ticket ≥ ticket threshold (70)
        findings=[],
        validated_phase=PlanningPhase.TICKET_EXPANSION,
    )
    resp = compose_response(
        variant=GuidanceVariant.PHASE_STATUS_REPORT,
        session=_session(current_phase=PlanningPhase.TICKET_EXPANSION.value),
        spec_full=_spec_full_with_tickets("t1", "t2"),
        validator_output=output,
        validator_config=VALIDATOR_CONFIG,
    )
    assert resp.gate_result == "pass"
    assert "gate pass" in resp.guidance
    assert "gate fail" not in resp.guidance


def test_status_report_shows_phase_gate_fail_when_a_ticket_is_short() -> None:
    # Symmetric: one ticket below threshold → the phase gate fails (spec-wide all-pass),
    # even if the composite gate_result happened to be 'pass'.
    output = ValidatorOutput(
        gate_result="pass",
        local_score=75.0,
        per_epic_score={},
        per_ticket_score={"t1": 100.0, "t2": 50.0},
        findings=[],
        validated_phase=PlanningPhase.TICKET_EXPANSION,
    )
    resp = compose_response(
        variant=GuidanceVariant.PHASE_STATUS_REPORT,
        session=_session(current_phase=PlanningPhase.TICKET_EXPANSION.value),
        spec_full=_spec_full_with_tickets("t1", "t2"),
        validator_output=output,
        validator_config=VALIDATOR_CONFIG,
    )
    assert resp.gate_result == "fail"
    assert "gate fail" in resp.guidance
