"""Parity tests for the audit row builders (``planning/audit/*`` / work item #17).

The three pure builders:

* :func:`build_action` — the ``planning_session_actions`` row. Pins the CRITICAL
  contract that ``guidance_variant`` + ``findings_categories`` are stamped at
  construction (the sole aggregate-fold stream source), the distinct/order-preserving
  category derivation, ``score = validator_output.local_score``, the dedicated audit
  columns, and the flat catch-all ``payload``.
* :func:`build_transition` — the ``planning_phase_transitions`` row fields.
* :func:`map_path_to_operation` — the finding-path → fix-operation table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from specsmither.domain.enums import (
    ActorType,
    FindingCategory,
    GuidanceVariant,
    Outcome,
    PlanningPhase,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import (
    MapperContext,
    build_action,
    build_transition,
    map_path_to_operation,
)
from specsmither.lifecycle.ports import ValidatorFinding, ValidatorOutput

NOW = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()


def _clock() -> datetime:
    return NOW


def _ids(prefix: str = "id") -> Any:
    counter = {"n": 0}

    def gen() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return gen


def _finding(category: Any, *, entity_id: str | None = None) -> ValidatorFinding:
    return ValidatorFinding(
        category=category, message="m", severity="finding", entity_id=entity_id
    )


def _output(**overrides: Any) -> ValidatorOutput:
    base: dict[str, Any] = {
        "gate_result": "fail",
        "local_score": 0.4,
        "per_epic_score": {},
        "per_ticket_score": {},
        "findings": [],
        "validated_phase": PlanningPhase.PLANNING_SPEC,
    }
    base.update(overrides)
    return ValidatorOutput(**base)


# --------------------------------------------------------------------------- #
# build_action                                                                #
# --------------------------------------------------------------------------- #


def test_build_action_stamps_columns_variant_categories_and_score() -> None:
    action = build_action(
        session_id="s-1",
        operation="update_spec",
        phase=PlanningPhase.PLANNING_SPEC,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        validator_output=_output(
            local_score=0.4,
            findings=[_finding(FindingCategory.RUBRIC), _finding(FindingCategory.COUNT)],
        ),
        guidance_variant=GuidanceVariant.GATE_FAILED,
        id_generator=_ids("act"),
        clock=_clock,
    )

    # Dedicated audit columns (enum values, not members).
    assert action["id"] == "act-1"
    assert action["planning_session_id"] == "s-1"
    assert action["operation"] == "update_spec"
    assert action["phase"] == "planning_spec"
    assert action["outcome"] == "success"
    assert action["actor"] == "agent"
    assert action["created_at"] == NOW_ISO

    # The two stream-fold sources + the score.
    assert action["guidance_variant"] == "gate_failed"
    assert action["findings_categories"] == ["rubric", "count"]
    assert action["score"] == 0.4


def test_build_action_findings_categories_are_distinct_order_preserving() -> None:
    action = build_action(
        session_id="s-1",
        operation="create_epic",
        phase=PlanningPhase.EPIC_DECOMPOSITION,
        outcome=Outcome.DENIED,
        actor=ActorType.AGENT,
        validator_output=_output(
            findings=[
                _finding(FindingCategory.CROSS_CUT),
                _finding(FindingCategory.RUBRIC),
                _finding(FindingCategory.CROSS_CUT),
            ],
        ),
    )
    # Distinct, first-seen order preserved.
    assert action["findings_categories"] == ["cross-cut", "rubric"]


def test_build_action_omits_score_and_categories_without_validator_output() -> None:
    action = build_action(
        session_id="s-1",
        operation="phase_advance",
        phase=PlanningPhase.PLANNING_SPEC,
        outcome=Outcome.SUCCESS,
        actor=ActorType.HUMAN,
    )
    assert "score" not in action
    assert "findings_categories" not in action
    assert "guidance_variant" not in action
    assert action["actor"] == "human"


def test_build_action_omits_categories_when_no_findings_but_keeps_score() -> None:
    action = build_action(
        session_id="s-1",
        operation="update_spec",
        phase=PlanningPhase.PLANNING_SPEC,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        validator_output=_output(local_score=0.9, findings=[]),
        guidance_variant=GuidanceVariant.GATE_PASSED,
    )
    assert action["score"] == 0.9
    assert "findings_categories" not in action
    assert action["guidance_variant"] == "gate_passed"


def test_build_action_catch_all_payload_merges_op_payload_and_audit_extras() -> None:
    action = build_action(
        session_id="s-1",
        operation="update_ticket",
        phase=PlanningPhase.TICKET_EXPANSION,
        outcome=Outcome.DENIED,
        actor=ActorType.AGENT,
        payload={"id": "ticket-9", "fields": {"title": "T", "description": "D"}},
        deny_reason="count_below_min",
        per_entity_scores_after={"ticket-9": 0.5},
        id_generator=_ids("act"),
        clock=_clock,
    )
    payload = action["payload"]
    # Op-payload keys are spread flat (the aggregate fold reads them off `payload`).
    assert payload["id"] == "ticket-9"
    assert payload["fields"] == {"title": "T", "description": "D"}
    # Derived entity fields (extractEntityFields).
    assert payload["entity_type"] == "ticket"
    assert payload["entity_id"] == "ticket-9"
    assert payload["fields_changed"] == ["title", "description"]
    # Audit extras the M0 fold reads off the same dict.
    assert payload["deny_reason"] == "count_below_min"
    assert payload["per_entity_scores_after"] == {"ticket-9": 0.5}


def test_build_action_entity_id_falls_back_to_blueprint_id() -> None:
    action = build_action(
        session_id="s-1",
        operation="link_blueprint_to_tickets",
        phase=PlanningPhase.CROSS_VALIDATION,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        payload={"blueprint_id": "bp-3", "ticketIds": ["t-1"]},
    )
    assert action["payload"]["entity_type"] == "blueprint"
    assert action["payload"]["entity_id"] == "bp-3"


def test_build_action_no_payload_omits_catch_all() -> None:
    action = build_action(
        session_id="s-1",
        operation="get_planning_status",
        phase=PlanningPhase.PLANNING_SPEC,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
    )
    assert "payload" not in action


# --------------------------------------------------------------------------- #
# build_transition                                                            #
# --------------------------------------------------------------------------- #


def test_build_transition_fields() -> None:
    transition = build_transition(
        session_id="s-1",
        from_phase=PlanningPhase.PLANNING_SPEC,
        to_phase=PlanningPhase.EPIC_DECOMPOSITION,
        trigger=TransitionTrigger.HUMAN_APPROVE,
        actor=ActorType.HUMAN,
        id_generator=_ids("trn"),
        clock=_clock,
    )
    assert transition == {
        "id": "trn-1",
        "planning_session_id": "s-1",
        "from_phase": "planning_spec",
        "to_phase": "epic_decomposition",
        "trigger": "human_approve",
        "actor": "human",
        "created_at": NOW_ISO,
    }


def test_build_transition_defaults_mint_ulid_and_now() -> None:
    transition = build_transition(
        session_id="s-1",
        from_phase="cross_validation",
        to_phase="cross_validation",
        trigger="ai_agent",
        actor="agent",
    )
    # 26-char ULID + a real ISO timestamp when no injectors are supplied.
    assert len(transition["id"]) == 26
    assert "T" in transition["created_at"]
    assert transition["trigger"] == "ai_agent"


# --------------------------------------------------------------------------- #
# map_path_to_operation                                                       #
# --------------------------------------------------------------------------- #


def test_map_path_spec_and_entity_levels() -> None:
    assert map_path_to_operation("/spec/title") == "update_spec"
    assert map_path_to_operation("/epics/epic-1/summary") == "update_epic"
    assert map_path_to_operation("/epics/epic-1/tickets/t-1/title") == "update_ticket"
    assert map_path_to_operation("/tickets/t-1") == "update_ticket"
    assert map_path_to_operation("/blueprints/bp-1/category") == "update_blueprint"


def test_map_path_ticket_dependencies_and_blueprint_refs_precede_generic_ticket() -> None:
    assert (
        map_path_to_operation("/epics/e-1/tickets/t-1/dependencies")
        == "create_dependencies"
    )
    assert (
        map_path_to_operation("/epics/e-1/tickets/t-1/blueprintReferences")
        == "link_blueprint_to_tickets"
    )


def test_map_path_cross_validation_by_op() -> None:
    assert (
        map_path_to_operation("/cross-validation/by-op/add_dependencies")
        == "create_dependencies"
    )
    assert map_path_to_operation("/cross-validation/by-op/set_metadata") == "update_spec"
    # Generic cross-validation enrichment fallback.
    assert (
        map_path_to_operation("/cross-validation/broken-reference")
        == "link_blueprint_to_tickets"
    )


def test_map_path_count_below_min_category_guard() -> None:
    # With the matching category context → the create_* op.
    assert (
        map_path_to_operation(
            "/structural/epics", MapperContext(category="count_below_min")
        )
        == "create_epic"
    )
    # Without the category context the guarded entry is skipped → structural catch-all.
    assert map_path_to_operation("/structural/epics") == "update_spec"


def test_map_path_unknown_and_empty_return_none() -> None:
    assert map_path_to_operation("") is None
    assert map_path_to_operation("/totally/unknown/path") is None
