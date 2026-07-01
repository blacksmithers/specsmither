"""Parity tests for the WritePlan builders + decompose-on-write (work item #8).

These pin the flat (one-transaction) M0 WritePlan shapes the L4 verbs build:

* SPS create — the ``status='planning'`` ``SpecMutation`` only when the spec was draft.
* the ``update_ticket`` APS decompose — child arrays → ``RelatedReplace`` items with
  deterministic child ids + a flat residual ``SpecMutation`` (the arrays are stripped).
* the APS ``SessionUpdate`` single-source-of-truth fix (``last_validator_output`` +
  ``last_score`` + ``last_gate_result`` + ``last_validated_at``).
* the gate's entity-score writes → ``ScoreDatapointAppend`` items ONLY (no row fusion,
  no ``EntityScoreUpdate``).
* approveHandover TERMINAL (spec ``ready`` + session ``closed``) / non-terminal advance.
* rejectHandoverWithFeedback stashes ``pending_human_feedback``; gps feedback-consume
  nulls it.
"""

from __future__ import annotations

from typing import Any

from crucible.models import Specification

from specsmither.adapters.write_plan_executor import (
    ActionAppend,
    EntityDelete,
    RecordTransition,
    RelatedDelete,
    RelatedPut,
    RelatedReplace,
    ScoreDatapointAppend,
    SessionCreate,
    SessionUpdate,
    SpecMutation,
)
from specsmither.domain.enums import DatapointTrigger, PlanningPhase
from specsmither.lifecycle.gate import EntityScoreWrite
from specsmither.lifecycle.ports import EpicFull, SpecFull, TicketRef, ValidatorOutput
from specsmither.lifecycle.write_plan import (
    ApsMutation,
    ApsRollback,
    build_approve_handover_write_plan,
    build_aps_denied_write_plan,
    build_aps_success_write_plan,
    build_cps_success_write_plan,
    build_create_persist,
    build_gps_feedback_consume_write_plan,
    build_gps_read_only_write_plan,
    build_op_write_items,
    build_reject_handover_with_feedback_write_plan,
    build_reject_handover_write_plan,
    build_sps_create_write_plan,
    build_sps_resume_write_plan,
    extract_mutation_target,
    resolve_aps_mutation,
)

NOW_ISO = "2025-01-01T00:00:00+00:00"


def _ids(prefix: str = "id") -> Any:
    counter = {"n": 0}

    def gen() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return gen


def _action(action_id: str = "act-1", *, created_at: str = NOW_ISO) -> dict[str, Any]:
    return {"id": action_id, "planning_session_id": "s-1", "created_at": created_at}


def _output(**overrides: Any) -> ValidatorOutput:
    base: dict[str, Any] = {
        "gate_result": "pass",
        "local_score": 0.92,
        "per_epic_score": {},
        "per_ticket_score": {},
        "findings": [],
        "validated_phase": PlanningPhase.PLANNING_SPEC,
    }
    base.update(overrides)
    return ValidatorOutput(**base)


def _spec_full(*, spec_id: str = "spec-1", epics: list[EpicFull] | None = None) -> SpecFull:
    spec = Specification(id=spec_id, title="T", project_id="proj-1", status="planning")
    return SpecFull(spec=spec, epics=epics or [], blueprints=[])


def _kinds(plan: Any) -> list[str]:
    return [item.kind for item in plan.items]


# --------------------------------------------------------------------------- #
# SPS create — status='planning' only when draft                              #
# --------------------------------------------------------------------------- #


def test_sps_create_emits_status_planning_spec_mutation_only_when_draft() -> None:
    session = {"id": "s-1", "specification_id": "spec-1", "status": "active"}
    plan = build_sps_create_write_plan(
        spec_id="spec-1", spec_status="draft", session=session, action=_action()
    )
    assert _kinds(plan) == ["specMutation", "sessionCreate", "actionAppend"]
    mutation = plan.items[0]
    assert isinstance(mutation, SpecMutation)
    assert mutation.entity_type == "spec"
    assert mutation.entity_id == "spec-1"
    assert mutation.fields == {"status": "planning"}
    assert isinstance(plan.items[1], SessionCreate)
    assert isinstance(plan.items[2], ActionAppend)


def test_sps_create_omits_spec_mutation_when_already_planning() -> None:
    session = {"id": "s-1", "specification_id": "spec-1", "status": "active"}
    plan = build_sps_create_write_plan(
        spec_id="spec-1", spec_status="planning", session=session, action=_action()
    )
    # No status flip — the session-create + audit append only.
    assert _kinds(plan) == ["sessionCreate", "actionAppend"]


# --------------------------------------------------------------------------- #
# SPS resume — fresh validator output persisted only on a fresh validate      #
# --------------------------------------------------------------------------- #


def test_sps_resume_persists_fresh_validator_output_when_fresh() -> None:
    plan = build_sps_resume_write_plan(
        session_id="s-1",
        action=_action(),
        fresh_validator_output=_output(local_score=0.5, gate_result="fail"),
        fresh_validated_at=NOW_ISO,
    )
    assert _kinds(plan) == ["sessionUpdate", "actionAppend"]
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["last_score"] == 0.5
    assert update.fields["last_gate_result"] == "fail"
    assert update.fields["last_validated_at"] == NOW_ISO
    assert update.fields["last_validator_output"]["validated_phase"] == "planning_spec"


def test_sps_resume_omits_validator_fields_on_cache_hit() -> None:
    plan = build_sps_resume_write_plan(session_id="s-1", action=_action())
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert "last_validator_output" not in update.fields
    assert "last_score" not in update.fields
    assert update.fields["last_action_at"] == NOW_ISO


# --------------------------------------------------------------------------- #
# update_ticket APS decompose                                                 #
# --------------------------------------------------------------------------- #


def _update_ticket_payload() -> dict[str, Any]:
    return {
        "id": "t-1",
        "fields": {
            "title": "Build the thing",
            "complexity": "moderate",
            "acceptanceCriteria": [
                {"given": "g0", "when": "w0", "then": "th0"},
                {"given": "g1", "when": "w1", "then": "th1"},
            ],
            "implementationSteps": ["step a", "step b"],
            "testSpecification": {
                "testTypes": ["unit", "integration"],
                "qualityGates": ["lint"],
                "coverageTarget": 80,
            },
            "filesToBeCreated": ["src/a.py"],
            "filesToBeModified": ["src/b.py"],
        },
    }


def test_update_ticket_decomposes_arrays_into_related_replace_and_flat_mutation() -> None:
    mutation = resolve_aps_mutation(
        "update_ticket", _update_ticket_payload(), _spec_full()
    )
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=mutation,
        action=_action(),
        gate_result="pass",
        validator_output=_output(),
        entity_score_writes=[],
        prev_session_status="active",
    )

    # The flat SpecMutation carries the residual columns; the child arrays are gone.
    spec_mutation = next(i for i in plan.items if isinstance(i, SpecMutation))
    assert spec_mutation.entity_type == "ticket"
    assert spec_mutation.entity_id == "t-1"
    assert spec_mutation.fields["title"] == "Build the thing"
    assert spec_mutation.fields["complexity"] == "moderate"
    # testSpecification's flat fields are hoisted onto the ticket row.
    assert spec_mutation.fields["qualityGates"] == ["lint"]
    assert spec_mutation.fields["coverageTarget"] == 80
    # The decomposed arrays are NOT on the flat columns.
    for stripped in (
        "acceptanceCriteria",
        "implementationSteps",
        "testSpecification",
        "filesToBeCreated",
        "filesToBeModified",
    ):
        assert stripped not in spec_mutation.fields

    replaces = {i.table: i for i in plan.items if isinstance(i, RelatedReplace)}
    assert set(replaces) == {
        "acceptance_criteria",
        "implementation_steps",
        "ticket_tests",
        "ticket_file_changes",
    }
    for replace in replaces.values():
        assert replace.parent_field == "ticket_id"
        assert replace.parent_id == "t-1"

    # Deterministic child ids.
    assert [r["id"] for r in replaces["acceptance_criteria"].items] == ["t-1-ac-0", "t-1-ac-1"]
    assert replaces["acceptance_criteria"].items[1] == {
        "ticket_id": "t-1",
        "id": "t-1-ac-1",
        "given": "g1",
        "when": "w1",
        "then": "th1",
        "order": 2,
    }
    assert [r["id"] for r in replaces["implementation_steps"].items] == ["t-1-is-0", "t-1-is-1"]
    assert replaces["implementation_steps"].items[0]["text"] == "step a"
    assert [r["id"] for r in replaces["ticket_tests"].items] == ["t-1-tt-unit", "t-1-tt-integration"]
    assert replaces["ticket_tests"].items[0] == {
        "ticket_id": "t-1",
        "id": "t-1-tt-unit",
        "test_type": "unit",
        "order": 0,
    }
    assert {r["id"] for r in replaces["ticket_file_changes"].items} == {
        "t-1-fc-toBeCreated-0",
        "t-1-fc-toBeModified-0",
    }
    file_kinds = {r["kind"] for r in replaces["ticket_file_changes"].items}
    assert file_kinds == {"toBeCreated", "toBeModified"}


def test_update_ticket_relatedreplace_items_appear_after_core_items() -> None:
    mutation = resolve_aps_mutation(
        "update_ticket", _update_ticket_payload(), _spec_full()
    )
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=mutation,
        action=_action(),
        gate_result="pass",
        validator_output=_output(),
        entity_score_writes=[],
        prev_session_status="active",
    )
    kinds = _kinds(plan)
    # specMutation, sessionUpdate, actionAppend, then the relatedReplace extras.
    assert kinds[:3] == ["specMutation", "sessionUpdate", "actionAppend"]
    assert all(k == "relatedReplace" for k in kinds[3:])


# --------------------------------------------------------------------------- #
# APS SessionUpdate single-source-of-truth + score datapoints (no fusion)     #
# --------------------------------------------------------------------------- #


def test_aps_success_session_update_persists_validator_output_score_and_gate() -> None:
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=ApsMutation("spec", "spec-1", {"title": "New title"}),
        action=_action(created_at="2025-06-01T00:00:00+00:00"),
        gate_result="pass",
        validator_output=_output(local_score=0.92),
        entity_score_writes=[],
        prev_session_status="active",
    )
    update = next(i for i in plan.items if isinstance(i, SessionUpdate))
    assert update.fields["last_gate_result"] == "pass"
    assert update.fields["last_score"] == 0.92
    assert update.fields["last_validated_at"] == "2025-06-01T00:00:00+00:00"
    assert update.fields["last_action_at"] == "2025-06-01T00:00:00+00:00"
    assert update.fields["last_validator_output"]["local_score"] == 0.92
    # An active-session agent edit does not write a status field (stays active).
    assert "status" not in update.fields
    assert "current_phase" not in update.fields


def test_aps_success_entity_scores_become_score_datapoints_only_no_fusion() -> None:
    writes = [
        EntityScoreWrite(
            entity_type="spec",
            entity_id="spec-1",
            score=0.92,
            trigger=DatapointTrigger.METADATA_UPDATED,
        ),
        EntityScoreWrite(
            entity_type="epic",
            entity_id="epic-1",
            score=0.8,
            trigger=DatapointTrigger.CROSS_VAL_RECOMPUTE,
        ),
    ]
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=ApsMutation("spec", "spec-1", {"title": "New title"}),
        action=_action("act-7"),
        gate_result="pass",
        validator_output=_output(),
        entity_score_writes=writes,
        prev_session_status="active",
    )

    # NO row fusion: the spec's score is not merged onto the SpecMutation fields.
    spec_mutation = next(i for i in plan.items if isinstance(i, SpecMutation))
    assert "specScore" not in spec_mutation.fields
    assert "spec_score" not in spec_mutation.fields
    assert "score" not in spec_mutation.fields

    # NO EntityScoreUpdate items — only ScoreDatapointAppend (one per write).
    assert "entityScoreUpdate" not in _kinds(plan)
    datapoints = [i for i in plan.items if isinstance(i, ScoreDatapointAppend)]
    assert len(datapoints) == 2
    spec_dp = datapoints[0]
    assert spec_dp.entity_type == "spec"
    assert spec_dp.entity_id == "spec-1"
    assert spec_dp.score == 0.92
    assert spec_dp.trigger == "metadata_updated"
    assert spec_dp.trigger_action_id == "act-7"
    # The executor's deterministic datapoint id format.
    expected_id = f"{spec_dp.trigger_action_id}#{spec_dp.entity_id}#{spec_dp.trigger}"
    assert expected_id == "act-7#spec-1#metadata_updated"
    assert datapoints[1].trigger == "cross_val_recompute"


def test_aps_success_human_edit_on_awaiting_stays_awaiting() -> None:
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=ApsMutation("ticket", "t-1", {"title": "x"}),
        action=_action(),
        gate_result="pass",
        validator_output=_output(),
        entity_score_writes=[],
        prev_session_status="awaiting_human_review",
        actor="human",
    )
    update = next(i for i in plan.items if isinstance(i, SessionUpdate))
    # A human edit on an awaiting session re-scores in place — no status flip.
    assert "status" not in update.fields


def test_aps_success_agent_edit_on_awaiting_flips_to_active() -> None:
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=ApsMutation("ticket", "t-1", {"title": "x"}),
        action=_action(),
        gate_result="pass",
        validator_output=_output(),
        entity_score_writes=[],
        prev_session_status="awaiting_human_review",
        actor="agent",
    )
    update = next(i for i in plan.items if isinstance(i, SessionUpdate))
    assert update.fields["status"] == "active"


def test_aps_success_late_op_rollback_records_transition_and_rewinds_phase() -> None:
    transition = {"id": "trn-1", "from_phase": "cross_validation", "to_phase": "epic_decomposition"}
    plan = build_aps_success_write_plan(
        session_id="s-1",
        mutation=ApsMutation("epic", "epic-1", {"title": "x"}),
        action=_action(),
        gate_result="fail",
        validator_output=_output(gate_result="fail"),
        entity_score_writes=[],
        prev_session_status="active",
        rollback=ApsRollback(
            effective_phase=PlanningPhase.EPIC_DECOMPOSITION, transition=transition
        ),
    )
    update = next(i for i in plan.items if isinstance(i, SessionUpdate))
    assert update.fields["current_phase"] == "epic_decomposition"
    record = next(i for i in plan.items if isinstance(i, RecordTransition))
    assert record.transition == transition


def test_aps_denied_is_audit_only() -> None:
    plan = build_aps_denied_write_plan(session_id="s-1", action=_action())
    assert _kinds(plan) == ["actionAppend", "sessionUpdate"]
    update = plan.items[1]
    assert isinstance(update, SessionUpdate)
    # No entity mutation, no status/phase change.
    assert "status" not in update.fields
    assert "current_phase" not in update.fields


# --------------------------------------------------------------------------- #
# decompose helpers — deletes + dependencies + create persist                 #
# --------------------------------------------------------------------------- #


def test_build_op_write_items_delete_emits_entity_delete() -> None:
    items = build_op_write_items("delete_ticket", {"id": "t-9"})
    assert items.delete_items == [EntityDelete("ticket", "t-9")]
    assert items.extra_items == []


def test_build_op_write_items_create_dependencies_emits_deterministic_edges() -> None:
    items = build_op_write_items(
        "create_dependencies",
        {"dependencies": [{"fromTicketId": "t-1", "toTicketId": "t-2"}]},
    )
    assert len(items.extra_items) == 1
    put = items.extra_items[0]
    assert isinstance(put, RelatedPut)
    assert put.table == "ticket_dependencies"
    assert put.item == {
        "id": "t-1--requires--t-2",
        "ticket_id": "t-1",
        "depends_on_id": "t-2",
        "type": "requires",
    }


def test_build_op_write_items_delete_dependencies_emits_related_delete() -> None:
    items = build_op_write_items("delete_dependencies", {"dependencyIds": ["t-1--requires--t-2"]})
    assert items.extra_items == [
        RelatedDelete(table="ticket_dependencies", key={"id": "t-1--requires--t-2"})
    ]


def test_build_create_persist_mints_id_and_full_columns_for_create_epic() -> None:
    epics = [
        EpicFull(id="e-1", specification_id="spec-1", title="A", tickets=[]),
    ]
    create = build_create_persist(
        "create_epic",
        {"title": "New epic", "objective": "do it"},
        _spec_full(epics=epics),
        id_generator=_ids("epic"),
    )
    assert create is not None
    assert create.entity_type == "epic"
    assert create.entity_id == "epic-1"
    assert create.fields["specification_id"] == "spec-1"
    # Numbering derives from the PRE-mutation count (1 existing → number/order 2).
    assert create.fields["epic_number"] == 2
    assert create.fields["order"] == 2
    assert create.fields["title"] == "New epic"
    assert create.fields["status"] == "todo"
    assert create.fields["planning_type"] == "planning"


def test_build_create_persist_returns_none_for_non_create_op() -> None:
    assert build_create_persist("update_spec", {"fields": {}}, _spec_full()) is None


def test_resolve_aps_mutation_create_ticket_targets_minted_id() -> None:
    epics = [
        EpicFull(
            id="e-1",
            specification_id="spec-1",
            title="A",
            tickets=[TicketRef(id="t-1", epic_id="e-1", title="x")],
        )
    ]
    mutation = resolve_aps_mutation(
        "create_ticket",
        {"epicId": "e-1", "title": "Fresh"},
        _spec_full(epics=epics),
        id_generator=_ids("ticket"),
    )
    assert mutation.entity_type == "ticket"
    assert mutation.entity_id == "ticket-1"
    assert mutation.fields["epic_id"] == "e-1"
    # One existing ticket on the epic → number/order 2.
    assert mutation.fields["ticket_number"] == 2


def test_extract_mutation_target_update_spec_and_blueprint_fallback() -> None:
    assert extract_mutation_target("update_spec", {"fields": {}}, "spec-1") == ("spec", "spec-1")
    assert extract_mutation_target(
        "update_blueprint", {"blueprintId": "bp-3"}, "spec-1"
    ) == ("blueprint", "bp-3")


# --------------------------------------------------------------------------- #
# CPS success                                                                 #
# --------------------------------------------------------------------------- #


def test_cps_success_parks_session_awaiting_with_transition() -> None:
    transition = {"id": "trn-1", "from_phase": "cross_validation", "to_phase": "cross_validation"}
    plan = build_cps_success_write_plan(
        session_id="s-1", action=_action(), transition=transition
    )
    assert _kinds(plan) == ["sessionUpdate", "recordTransition", "actionAppend"]
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["status"] == "awaiting_human_review"


# --------------------------------------------------------------------------- #
# Handover                                                                    #
# --------------------------------------------------------------------------- #


def test_approve_handover_terminal_sets_spec_ready_and_session_closed() -> None:
    plan = build_approve_handover_write_plan(
        session_id="s-1",
        spec_id="spec-1",
        transitioned_phase=PlanningPhase.CROSS_VALIDATION,
        is_terminal=True,
        now=NOW_ISO,
        actions=[_action("act-complete"), _action("act-closed")],
    )
    assert _kinds(plan) == ["specMutation", "sessionUpdate", "actionAppend", "actionAppend"]
    spec_mutation = plan.items[0]
    assert isinstance(spec_mutation, SpecMutation)
    assert spec_mutation.entity_type == "spec"
    assert spec_mutation.fields == {"status": "ready"}
    update = plan.items[1]
    assert isinstance(update, SessionUpdate)
    assert update.fields["status"] == "closed"
    assert update.fields["closed_at"] == NOW_ISO
    assert update.fields["last_transition_trigger"] == "human_approve"


def test_approve_handover_non_terminal_advances_phase_with_transition() -> None:
    transition = {"id": "trn-1", "from_phase": "planning_spec", "to_phase": "epic_decomposition"}
    plan = build_approve_handover_write_plan(
        session_id="s-1",
        spec_id="spec-1",
        transitioned_phase=PlanningPhase.EPIC_DECOMPOSITION,
        is_terminal=False,
        now=NOW_ISO,
        actions=[_action("act-advance"), _action("act-approve")],
        transition=transition,
    )
    assert _kinds(plan) == ["sessionUpdate", "recordTransition", "actionAppend", "actionAppend"]
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["status"] == "active"
    assert update.fields["current_phase"] == "epic_decomposition"
    # The spec is NOT touched on a non-terminal advance.
    assert not any(isinstance(i, SpecMutation) for i in plan.items)


def test_reject_handover_with_feedback_stashes_pending_human_feedback() -> None:
    feedback = {
        "content": "please add error handling",
        "recorded_at": NOW_ISO,
        "recorded_by_user_id": "local",
    }
    plan = build_reject_handover_with_feedback_write_plan(
        session_id="s-1", action=_action(), feedback=feedback, now=NOW_ISO
    )
    assert _kinds(plan) == ["sessionUpdate", "actionAppend"]
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["status"] == "active"
    assert update.fields["pending_human_feedback"] == feedback
    assert update.fields["last_transition_trigger"] == "human_reject_with_feedback"
    # Phase unchanged.
    assert "current_phase" not in update.fields


def test_reject_handover_no_feedback_does_not_stash_feedback() -> None:
    plan = build_reject_handover_write_plan(session_id="s-1", action=_action(), now=NOW_ISO)
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["status"] == "active"
    assert update.fields["last_transition_trigger"] == "human_reject_no_feedback"
    assert "pending_human_feedback" not in update.fields


# --------------------------------------------------------------------------- #
# get_planning_status                                                         #
# --------------------------------------------------------------------------- #


def test_gps_feedback_consume_nulls_pending_human_feedback() -> None:
    plan = build_gps_feedback_consume_write_plan(session_id="s-1", action=_action())
    assert _kinds(plan) == ["sessionUpdate", "actionAppend"]
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    # The key is present with an explicit None so the executor clears the column.
    assert "pending_human_feedback" in update.fields
    assert update.fields["pending_human_feedback"] is None


def test_gps_read_only_bumps_last_read_at_when_requested() -> None:
    plan = build_gps_read_only_write_plan(
        session_id="s-1", action=_action(), bump_last_read_at=True, now=NOW_ISO
    )
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert update.fields["last_read_at"] == NOW_ISO


def test_gps_read_only_without_bump_omits_last_read_at() -> None:
    plan = build_gps_read_only_write_plan(session_id="s-1", action=_action())
    update = plan.items[0]
    assert isinstance(update, SessionUpdate)
    assert "last_read_at" not in update.fields
