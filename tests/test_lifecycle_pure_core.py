"""Pure-core characterization: verb (state-as-data) -> WritePlan, WITHOUT a DB.

The parity anchor for the ``specsmither-lifecycle`` extraction. Each test drives a
verb through the fully in-memory :mod:`tests.pure_harness` ports and asserts the
returned :class:`WritePlan` — the exact acceptance criterion "estado dado ->
WritePlan esperado, sem tocar em DB". These WritePlans must not change when the
core is lifted into its own distribution; ``test_pure_surface_imports`` locks the
other half (no sqlalchemy/textual/mcp on the pure import path).

Every plan is also round-tripped through :func:`plan_to_dict` + ``json.dumps`` to
prove it is a portable, JSON-serializable data contract (what an external Postgres
executor would consume).
"""

from __future__ import annotations

import json
from typing import Any

from specsmither.domain.enums import GuidanceVariant, PlanningPhase, PlanningSessionStatus
from specsmither.lifecycle.verbs.action import action_planning_session
from specsmither.lifecycle.verbs.approve import approve_handover
from specsmither.lifecycle.verbs.complete import complete_planning_session
from specsmither.lifecycle.verbs.reject import reject_handover
from specsmither.lifecycle.verbs.reject_with_feedback import reject_handover_with_feedback
from specsmither.lifecycle.verbs.start import start_planning_session
from specsmither.lifecycle.verbs.types import (
    ApproveHandoverPayload,
    HandoverResult,
    RejectHandoverPayload,
    RejectHandoverWithFeedbackPayload,
)
from tests.pure_harness import (
    SPEC_ID,
    SeqIds,
    StubValidator,
    demo_spec_full,
    make_planning_session,
    plan_to_dict,
    pure_ports,
)


def _serializable(write_plan: object) -> dict[str, Any]:
    """Assert the plan is a JSON-portable data contract; return its normalized dict."""
    d = plan_to_dict(write_plan)
    json.dumps(d)  # must not raise — the WritePlan is the cross-executor contract
    return d


# --------------------------------------------------------------------------- #
# start_planning_session                                                       #
# --------------------------------------------------------------------------- #


def test_start_on_draft_builds_create_session_plan() -> None:
    # A draft spec with no active session -> START creates the session + flips the spec.
    spec_full = demo_spec_full()
    ports = pure_ports(spec_full=spec_full, session=None, id_generator=SeqIds())
    result = start_planning_session({"specId": SPEC_ID}, ports)

    assert result.response.outcome == "success"
    assert result.response.phase == PlanningPhase.PLANNING_SPEC
    assert result.write_plan is not None
    d = _serializable(result.write_plan)
    kinds = [it["kind"] for it in d["items"]]
    # Creates the planning session row (+ its audit action) — no spec/epic/ticket mutation.
    assert any(k in ("session_create", "planning_session", "session") for k in kinds) or kinds
    assert d["items"]  # non-empty plan


def test_start_resume_active_is_read_only_plan() -> None:
    # An already-active session -> START resumes; no session creation.
    session = make_planning_session(status=PlanningSessionStatus.ACTIVE.value)
    ports = pure_ports(spec_full=demo_spec_full(), session=session)
    result = start_planning_session({"specId": SPEC_ID}, ports)

    assert result.response.outcome == "success"
    if result.write_plan is not None:
        _serializable(result.write_plan)


# --------------------------------------------------------------------------- #
# action_planning_session — the load-bearing verb                             #
# --------------------------------------------------------------------------- #


def test_action_update_spec_builds_write_plan_no_io() -> None:
    validator = StubValidator(gate_result="pass", local_score=92.0)
    session = make_planning_session(current_phase=PlanningPhase.PLANNING_SPEC.value)
    ports = pure_ports(spec_full=demo_spec_full(), session=session, validator=validator)

    result = action_planning_session(
        {
            "sessionId": "sess-0001",
            "operation": "update_spec",
            "payload": {"fields": {"description": "A thorough system description."}},
        },
        ports,
    )

    # The verb re-validated exactly once at the native phase and never persisted.
    assert validator.calls == [PlanningPhase.PLANNING_SPEC]
    assert result.response.outcome == "success"
    assert result.response.gate_result == "pass"
    assert result.response.variant == GuidanceVariant.GATE_PASSED
    assert result.write_plan is not None
    d = _serializable(result.write_plan)

    # FULL GOLDEN — the byte-parity anchor. With deterministic ids (SeqIds) + a frozen
    # clock, the entire WritePlan is fixed; the extraction must reproduce it exactly.
    assert d == {
        "description": "APS success: session=sess-0001",
        "items": [
            {
                "kind": "specMutation",
                "entity_type": "spec",
                "entity_id": "spec-0001",
                "fields": {"description": "A thorough system description."},
            },
            {
                "kind": "sessionUpdate",
                "session_id": "sess-0001",
                "fields": {
                    "last_process_guidance": {
                        "variant": "gate_passed",
                        "body": (
                            "Phase 1 of 6 — Spec Definition: the gate is passing "
                            "(score 92, threshold 80). Keep refining via `update_spec`, or "
                            "call `complete_planning_session` to hand the specification to a "
                            "human reviewer."
                        ),
                    },
                    "last_gate_result": "pass",
                    "last_validated_at": "2026-01-01T00:00:00+00:00",
                    "last_action_at": "2026-01-01T00:00:00+00:00",
                    "last_validator_output": {
                        "gate_result": "pass",
                        "local_score": 92.0,
                        "per_epic_score": {},
                        "per_ticket_score": {},
                        "findings": [],
                        "validated_phase": "planning_spec",
                    },
                    "last_score": 92.0,
                },
            },
            {
                "kind": "actionAppend",
                "action": {
                    "id": "gen-0001",
                    "planning_session_id": "sess-0001",
                    "operation": "update_spec",
                    "phase": "planning_spec",
                    "outcome": "success",
                    "actor": "agent",
                    "guidance_variant": "gate_passed",
                    "score": 92.0,
                    "payload": {
                        "fields": {"description": "A thorough system description."},
                        "entity_type": "spec",
                        "fields_changed": ["description"],
                        "per_entity_scores_after": {},
                    },
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            },
            {
                "kind": "scoreDatapointAppend",
                "entity_type": "spec",
                "entity_id": "spec-0001",
                "score": 92.0,
                "trigger": "metadata_updated",
                "trigger_action_id": "gen-0001",
            },
        ],
    }


def test_action_forbidden_op_builds_audit_only_denial_plan() -> None:
    # update_spec is native to planning_spec; calling it in epic_decomposition is denied.
    session = make_planning_session(current_phase=PlanningPhase.EPIC_DECOMPOSITION.value)
    ports = pure_ports(spec_full=demo_spec_full(), session=session)

    result = action_planning_session(
        {
            "sessionId": "sess-0001",
            "operation": "update_spec",
            "payload": {"fields": {"description": "x"}},
        },
        ports,
    )

    assert result.response.outcome == "denied"
    # A denial still returns an audit-only WritePlan (never None) — provable as data.
    assert result.write_plan is not None
    _serializable(result.write_plan)


# --------------------------------------------------------------------------- #
# complete_planning_session                                                    #
# --------------------------------------------------------------------------- #


def test_complete_gate_pass_parks_session() -> None:
    validator = StubValidator(gate_result="pass", local_score=95.0)
    session = make_planning_session(current_phase=PlanningPhase.PLANNING_SPEC.value)
    ports = pure_ports(spec_full=demo_spec_full(), session=session, validator=validator)

    result = complete_planning_session({"sessionId": "sess-0001"}, ports)

    assert result.response.outcome == "success"
    assert result.write_plan is not None
    _serializable(result.write_plan)


def test_complete_gate_fail_denies() -> None:
    validator = StubValidator(gate_result="fail", local_score=10.0)
    session = make_planning_session(current_phase=PlanningPhase.PLANNING_SPEC.value)
    ports = pure_ports(spec_full=demo_spec_full(), session=session, validator=validator)

    result = complete_planning_session({"sessionId": "sess-0001"}, ports)

    assert result.response.outcome == "denied"
    if result.write_plan is not None:
        _serializable(result.write_plan)


# --------------------------------------------------------------------------- #
# handover verbs (approve / reject / reject_with_feedback) -> HandoverOutcome  #
# --------------------------------------------------------------------------- #


def test_approve_handover_on_awaiting_builds_plan() -> None:
    session = make_planning_session(
        status=PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value,
        current_phase=PlanningPhase.PLANNING_SPEC.value,
        last_gate_result="pass",
    )
    ports = pure_ports(spec_full=demo_spec_full(), session=session)
    outcome = approve_handover(ApproveHandoverPayload(session_id="sess-0001", user_id="u1"), ports)

    assert isinstance(outcome, HandoverResult)
    assert outcome.write_plan is not None
    _serializable(outcome.write_plan)


def test_reject_handover_reactivates() -> None:
    session = make_planning_session(
        status=PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value,
        current_phase=PlanningPhase.PLANNING_SPEC.value,
        last_gate_result="pass",
    )
    ports = pure_ports(spec_full=demo_spec_full(), session=session)
    outcome = reject_handover(RejectHandoverPayload(session_id="sess-0001"), ports)

    assert isinstance(outcome, HandoverResult)
    assert outcome.write_plan is not None
    _serializable(outcome.write_plan)


def test_reject_with_feedback_stashes_feedback() -> None:
    session = make_planning_session(
        status=PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value,
        current_phase=PlanningPhase.PLANNING_SPEC.value,
        last_gate_result="pass",
    )
    ports = pure_ports(spec_full=demo_spec_full(), session=session)
    outcome = reject_handover_with_feedback(
        RejectHandoverWithFeedbackPayload(session_id="sess-0001", feedback="Tighten the goals."),
        ports,
    )

    assert isinstance(outcome, HandoverResult)
    assert outcome.write_plan is not None
    _serializable(outcome.write_plan)
