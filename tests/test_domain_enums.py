"""Verbatim-value parity tests for the runtime/lifecycle enum vocabulary."""

from __future__ import annotations

from specsmither.domain.enums import (
    ActorType,
    ActualAction,
    DatapointTrigger,
    EpicStatus,
    ExpectedAction,
    FieldState,
    FileChangeKind,
    FileChangeStatus,
    FindingCategory,
    GateResult,
    GuidanceVariant,
    JustificationApproved,
    Outcome,
    PlanningPhase,
    PlanningSessionStatus,
    SpecStatus,
    TicketStatus,
    TransitionTrigger,
    WorkSessionStatus,
)


def test_ticket_status_values() -> None:
    assert [s.value for s in TicketStatus] == ["pending", "ready", "active", "done"]


def test_spec_status_has_all_eight_values() -> None:
    assert [s.value for s in SpecStatus] == [
        "draft",
        "planning",
        "ready",
        "in_progress",
        "ready_for_review",
        "in_review",
        "reviewed",
        "done",
    ]


def test_spec_status_review_states_retained() -> None:
    # Unreachable in SpecSmither but kept for the CRUD-freeze guards (#18).
    assert SpecStatus.READY_FOR_REVIEW == "ready_for_review"
    assert SpecStatus.IN_REVIEW == "in_review"
    assert SpecStatus.REVIEWED == "reviewed"


def test_strenum_members_equal_their_string_values() -> None:
    assert TicketStatus.PENDING == "pending"
    assert PlanningSessionStatus.AWAITING_HUMAN_REVIEW == "awaiting_human_review"
    assert WorkSessionStatus.COMPLETED == "completed"
    assert GateResult.PASS == "pass"
    assert Outcome.DENIED == "denied"
    # Constructing from the verbatim value resolves to the canonical member.
    assert TicketStatus("done") is TicketStatus.DONE


def test_planning_phase_values() -> None:
    assert [p.value for p in PlanningPhase] == [
        "planning_spec",
        "epic_decomposition",
        "epic_expansion",
        "ticket_decomposition",
        "ticket_expansion",
        "cross_validation",
        "planned",
    ]


def test_transition_trigger_values() -> None:
    assert TransitionTrigger.AUTO_INITIAL == "auto_initial"
    assert TransitionTrigger.HUMAN_REJECT_WITH_FEEDBACK == "human_reject_with_feedback"
    assert TransitionTrigger.HUMAN_REJECT_NO_FEEDBACK == "human_reject_no_feedback"


def test_datapoint_trigger_sampling() -> None:
    assert DatapointTrigger.METADATA_UPDATED == "metadata_updated"
    assert DatapointTrigger.BLUEPRINT_UNLINKED == "blueprint_unlinked"
    assert DatapointTrigger.CROSS_VAL_RECOMPUTE == "cross_val_recompute"


def test_guidance_variant_includes_m66_additions() -> None:
    values = {v.value for v in GuidanceVariant}
    assert {"denied", "gate_failed", "phase_complete"} <= values
    # M6.6 additions.
    assert {
        "phase_status_report",
        "awaiting_human_review_handover",
        "human_feedback_received",
        "human_rejected_no_feedback",
        "phase_advanced_after_approve",
        "session_closed",
    } <= values
    assert len(values) == 14


def test_finding_category_hyphenated_values() -> None:
    assert FindingCategory.CROSS_CUT == "cross-cut"
    assert FindingCategory.CROSS_VALIDATION == "cross-validation"
    assert FindingCategory.SCHEMA == "schema"


def test_field_state_values() -> None:
    assert [f.value for f in FieldState] == ["empty", "partial", "filled", "na"]


def test_expected_and_actual_action_values() -> None:
    assert [a.value for a in ExpectedAction] == ["create", "modify", "delete", "reference"]
    assert [a.value for a in ActualAction] == [
        "created",
        "modified",
        "deleted",
        "referenced",
        "absent",
    ]


def test_file_change_status_values() -> None:
    assert [s.value for s in FileChangeStatus] == ["matched", "missing", "mismatched", "extra"]


def test_justification_approved_values() -> None:
    assert [j.value for j in JustificationApproved] == ["pending", "approved", "rejected"]


def test_file_change_kind_camelcase_literals() -> None:
    assert [k.value for k in FileChangeKind] == [
        "toBeCreated",
        "toBeModified",
        "toBeDeleted",
        "toBeReferenced",
    ]


def test_epic_status_distinct_three_value_vocabulary() -> None:
    assert [e.value for e in EpicStatus] == ["todo", "in_progress", "completed"]


def test_actor_type_values() -> None:
    assert ActorType.AGENT == "agent"
    assert ActorType.HUMAN == "human"
