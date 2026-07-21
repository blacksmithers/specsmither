"""Parity tests for the 9 planning pre-checks (pre-checks/*.ts / A1 §1.4).

Each pre-check is a pure ``(…) -> Accepted | Denied`` guard. These tests pin:

* ``operation_allowed`` — forbidden / native / late + the fieldDeclarations
  rollback exemption.
* ``spec_status_check`` — every verb × status (the M7.9 24-case matrix shape).
* ``count_bounds`` — max/min breaches vs ok.
* ``cross_cut_references`` / ``cascade_rules`` — referrer blocks + cascade confirm.
* ``blueprint_epic_ratio`` — the ratio threshold.
* ``schema_validate`` — a malformed payload is denied; a good one accepted.
* ``validate_dependencies_batch`` — a cycle and a fully-deduped batch.
* ``gate_currently_passing`` — ALWAYS re-validates (no TTL trust).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible.models import Specification

from specsmither.db.models import PlanningSession
from specsmither.domain.enums import PlanningPhase, SpecStatus
from specsmither.lifecycle.ports import (
    BlueprintRef,
    EpicFull,
    SpecDependencyEdge,
    SpecFull,
    TicketRef,
    ValidatorFinding,
    ValidatorOutput,
)
from specsmither.lifecycle.prechecks import (
    Accepted,
    Denied,
    blueprint_epic_ratio,
    cascade_rules,
    count_bounds,
    cross_cut_references,
    gate_currently_passing,
    operation_allowed,
    run_dependencies_batch,
    schema_validate,
    spec_status_check,
    validate_dependencies_batch,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

# A validator config shaped like the resolved crucible `planning` config: the
# count bounds + ratio leaves the pre-checks read.
CONFIG: dict[str, Any] = {
    "structuralRequirements": {
        "arrayMaxCounts": {
            "specification": {"epics": {"default": 3}},
            "epic": {"tickets": {"default": 2}},
        },
        "arrayMinCounts": {
            "specification": {"epics": {"default": 1}},
            "epic": {"tickets": {"default": 1}},
        },
    },
    "crossValidation": {"ratios": {"blueprintToEpic": {"min": 0.5}}},
}


def _spec() -> Specification:
    return Specification(id="spec-1", projectId="proj-1", title="T", status="planning")


def _ticket(tid: str, epic_id: str, **extra: Any) -> TicketRef:
    return TicketRef(id=tid, epic_id=epic_id, title=tid, extra=extra)


def _epic(eid: str, tickets: list[TicketRef], **extra: Any) -> EpicFull:
    return EpicFull(
        id=eid,
        specification_id="spec-1",
        title=eid,
        tickets=tickets,
        extra=extra,
    )


def _spec_full(
    *,
    epics: list[EpicFull] | None = None,
    blueprints: list[BlueprintRef] | None = None,
    dependencies: list[SpecDependencyEdge] | None = None,
) -> SpecFull:
    return SpecFull(
        spec=_spec(),
        epics=epics or [],
        blueprints=blueprints or [],
        dependencies=dependencies or [],
    )


def _edge(frm: str, to: str) -> SpecDependencyEdge:
    return SpecDependencyEdge(from_ticket_id=frm, to_ticket_id=to)


class _FakeValidator:
    """A validator that returns a fixed output regardless of input."""

    def __init__(self, output: ValidatorOutput) -> None:
        self._output = output
        self.calls = 0

    def validate(
        self,
        spec_full: SpecFull,
        phase: PlanningPhase,
        config: Mapping[str, Any],
        language: str = "en",
    ) -> ValidatorOutput:
        self.calls += 1
        return self._output


def _output(gate_result: str, findings: list[ValidatorFinding] | None = None) -> ValidatorOutput:
    return ValidatorOutput(
        gate_result=gate_result,  # type: ignore[arg-type]
        local_score=1.0 if gate_result == "pass" else 0.0,
        per_epic_score={},
        per_ticket_score={},
        findings=findings or [],
        validated_phase=PlanningPhase.CROSS_VALIDATION,
    )


# --------------------------------------------------------------------------- #
# operation_allowed                                                           #
# --------------------------------------------------------------------------- #


def test_operation_allowed_forbidden() -> None:
    # update_spec is forbidden once past planning_spec.
    result = operation_allowed("update_spec", PlanningPhase.EPIC_DECOMPOSITION)
    assert isinstance(result, Denied)
    assert result.code == "current_phase_must_finish_first"
    assert result.context == {
        "operation": "update_spec",
        "current_phase": "epic_decomposition",
        "native_phase": "planning_spec",
    }


def test_operation_allowed_native_no_rollback() -> None:
    result = operation_allowed("update_spec", PlanningPhase.PLANNING_SPEC)
    assert result == Accepted(rollback=False)


def test_operation_allowed_late_sets_rollback() -> None:
    # update_epic is native to epic_expansion; calling it later (cross_validation)
    # is a late op -> rollback.
    result = operation_allowed("update_epic", PlanningPhase.CROSS_VALIDATION, {"fields": {"title": "x"}})
    assert result == Accepted(rollback=True)


def test_operation_allowed_late_update_with_field_declarations_still_rolls_back() -> None:
    # The fieldDeclarations-only carve-out is retired: a late update_* ALWAYS rolls back
    # (N/A justification is now the dedicated justify/unjustify op, native in every phase).
    only = operation_allowed(
        "update_ticket",
        PlanningPhase.CROSS_VALIDATION,
        {"id": "t1", "fields": {"fieldDeclarations": {"dependencies": "n/a"}}},
    )
    assert only == Accepted(rollback=True)
    both = operation_allowed(
        "update_ticket",
        PlanningPhase.CROSS_VALIDATION,
        {"id": "t1", "fields": {"fieldDeclarations": {}, "title": "x"}},
    )
    assert both == Accepted(rollback=True)


def test_operation_allowed_justify_is_native_never_rolls_back() -> None:
    # justify/unjustify are native in every planning phase (structural-neutral).
    for phase in (PlanningPhase.PLANNING_SPEC, PlanningPhase.CROSS_VALIDATION):
        assert operation_allowed("justify", phase) == Accepted(rollback=False)
        assert operation_allowed("unjustify", phase) == Accepted(rollback=False)


# --------------------------------------------------------------------------- #
# spec_status_check                                                           #
# --------------------------------------------------------------------------- #


def test_spec_status_sps_draft_auto_transition() -> None:
    result = spec_status_check("sps", SpecStatus.DRAFT)
    assert result == Accepted(auto_transition=True)


def test_spec_status_sps_planning() -> None:
    result = spec_status_check("sps", SpecStatus.PLANNING)
    assert result == Accepted(auto_transition=False)


def test_spec_status_sps_other_denied() -> None:
    result = spec_status_check("sps", SpecStatus.READY)
    assert isinstance(result, Denied)
    assert result.code == "spec_not_in_planning"
    assert result.context is not None
    assert result.context["actual_status"] == "ready"
    assert "reopen_specification" in result.context["recovery_hint"]


def test_spec_status_aps_requires_planning() -> None:
    assert spec_status_check("aps", SpecStatus.PLANNING) == Accepted()
    denied = spec_status_check("aps", SpecStatus.DRAFT)
    assert isinstance(denied, Denied)
    assert denied.code == "spec_not_in_planning"


def test_spec_status_cps_requires_planning() -> None:
    assert spec_status_check("cps", SpecStatus.PLANNING) == Accepted()
    denied = spec_status_check("cps", SpecStatus.DONE)
    assert isinstance(denied, Denied)
    assert denied.code == "spec_not_in_planning"


def test_spec_status_full_matrix_shape() -> None:
    # 8 statuses × 3 verbs: only the documented accept cells pass.
    for status in SpecStatus:
        for verb in ("sps", "aps", "cps"):
            result = spec_status_check(verb, status)
            if status is SpecStatus.PLANNING:
                assert result == Accepted()
            elif verb == "sps" and status is SpecStatus.DRAFT:
                assert result == Accepted(auto_transition=True)
            else:
                assert isinstance(result, Denied)


# --------------------------------------------------------------------------- #
# count_bounds                                                                #
# --------------------------------------------------------------------------- #


def test_count_bounds_create_epic_at_max_denied() -> None:
    full = _spec_full(epics=[_epic("e1", []), _epic("e2", []), _epic("e3", [])])
    result = count_bounds("create_epic", {"id": "new"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "max_epics_exceeded"
    assert result.context == {"current": 3, "max": 3}


def test_count_bounds_create_epic_ok() -> None:
    full = _spec_full(epics=[_epic("e1", []), _epic("e2", [])])
    assert count_bounds("create_epic", {"id": "new"}, full, CONFIG) == Accepted()


def test_count_bounds_create_ticket_at_max_denied() -> None:
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1"), _ticket("t2", "e1")])])
    result = count_bounds("create_ticket", {"id": "new", "epicId": "e1"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "max_tickets_per_epic_exceeded"


def test_count_bounds_delete_epic_at_min_denied() -> None:
    full = _spec_full(epics=[_epic("e1", [])])
    result = count_bounds("delete_epic", {"id": "e1"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "min_epics_not_met"


def test_count_bounds_delete_ticket_not_found() -> None:
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1")])])
    result = count_bounds("delete_ticket", {"id": "missing"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "ticket_not_found"


def test_count_bounds_delete_ticket_at_min_denied() -> None:
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1")])])
    result = count_bounds("delete_ticket", {"id": "t1"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "min_tickets_per_epic_not_met"


# --------------------------------------------------------------------------- #
# cross_cut_references                                                        #
# --------------------------------------------------------------------------- #


def test_cross_cut_blocks_when_other_epic_references_target() -> None:
    target = _epic("e1", [])
    referrer = _epic("e2", [], related_epic="e1")  # extra column references e1
    full = _spec_full(epics=[target, referrer])
    result = cross_cut_references("delete_epic", {"id": "e1"}, full)
    assert isinstance(result, Denied)
    assert result.code == "cross_cut_reference_exists"
    assert result.context == {"referrer_ids": ["e2"]}


def test_cross_cut_accepts_when_no_referrers() -> None:
    full = _spec_full(epics=[_epic("e1", []), _epic("e2", [])])
    assert cross_cut_references("delete_epic", {"id": "e1"}, full) == Accepted()


# --------------------------------------------------------------------------- #
# cascade_rules                                                               #
# --------------------------------------------------------------------------- #


def test_cascade_blocks_delete_ticket_with_referrers() -> None:
    full = _spec_full(
        epics=[_epic("e1", [_ticket("t1", "e1"), _ticket("t2", "e1")])],
        dependencies=[_edge("t2", "t1")],  # t2 depends on t1
    )
    result = cascade_rules("delete_ticket", {"id": "t1"}, full)
    assert isinstance(result, Denied)
    assert result.code == "cascade_not_confirmed"
    assert result.context is not None
    assert result.context["referrer_ids"] == ["t2"]


def test_cascade_allows_with_confirmation() -> None:
    full = _spec_full(
        epics=[_epic("e1", [_ticket("t1", "e1"), _ticket("t2", "e1")])],
        dependencies=[_edge("t2", "t1")],
    )
    result = cascade_rules(
        "delete_ticket", {"id": "t1", "cascadeRemoveDependencies": True}, full
    )
    assert result == Accepted()


def test_cascade_delete_epic_only_external_referrers_block() -> None:
    # t-in is depended on only by t-also-in (same epic) -> no external referrer.
    full = _spec_full(
        epics=[
            _epic("e1", [_ticket("t-in", "e1"), _ticket("t-also-in", "e1")]),
            _epic("e2", [_ticket("t-out", "e2")]),
        ],
        dependencies=[_edge("t-also-in", "t-in")],
    )
    assert cascade_rules("delete_epic", {"id": "e1"}, full) == Accepted()
    # Now an external ticket depends on an epic-1 ticket -> blocked.
    full2 = _spec_full(
        epics=[
            _epic("e1", [_ticket("t-in", "e1")]),
            _epic("e2", [_ticket("t-out", "e2")]),
        ],
        dependencies=[_edge("t-out", "t-in")],
    )
    result = cascade_rules("delete_epic", {"id": "e1"}, full2)
    assert isinstance(result, Denied)
    assert result.context is not None
    assert result.context["referrer_ids"] == ["t-out"]


# --------------------------------------------------------------------------- #
# blueprint_epic_ratio                                                        #
# --------------------------------------------------------------------------- #


def test_blueprint_ratio_violation_denied() -> None:
    # 2 epics, 1 blueprint; after delete -> 0/2 = 0 < 0.5 -> denied.
    full = _spec_full(
        epics=[_epic("e1", []), _epic("e2", [])],
        blueprints=[BlueprintRef(id="b1", specification_id="spec-1", title="B", category="c")],
    )
    result = blueprint_epic_ratio("delete_blueprint", {"id": "b1"}, full, CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "blueprint_epic_ratio_violated"
    assert result.context == {"current": 1, "epics": 2, "min_required": 1, "after_delete": 0}


def test_blueprint_ratio_ok_when_enough_remain() -> None:
    # 2 epics, 2 blueprints; after delete -> 1/2 = 0.5 >= 0.5 -> ok.
    full = _spec_full(
        epics=[_epic("e1", []), _epic("e2", [])],
        blueprints=[
            BlueprintRef(id="b1", specification_id="spec-1", title="B1", category="c"),
            BlueprintRef(id="b2", specification_id="spec-1", title="B2", category="c"),
        ],
    )
    assert blueprint_epic_ratio("delete_blueprint", {"id": "b1"}, full, CONFIG) == Accepted()


# --------------------------------------------------------------------------- #
# schema_validate                                                             #
# --------------------------------------------------------------------------- #


def test_schema_validate_rejects_malformed_payload() -> None:
    # create_epic requires a non-empty title.
    result = schema_validate("create_epic", {"description": "no title"})
    assert isinstance(result, Denied)
    assert result.code == "invalid_payload"
    assert result.context is not None
    assert result.context["errors"]


def test_schema_validate_rejects_empty_title() -> None:
    result = schema_validate("create_epic", {"title": ""})
    assert isinstance(result, Denied)
    assert result.code == "invalid_payload"


def test_schema_validate_accepts_good_payload() -> None:
    assert schema_validate("create_epic", {"title": "Real epic"}) == Accepted()


def test_schema_validate_get_planning_status_strict() -> None:
    assert schema_validate("get_planning_status", {}) == Accepted()
    denied = schema_validate("get_planning_status", {"unexpected": 1})
    assert isinstance(denied, Denied)


def test_schema_validate_synthetic_op_accepted() -> None:
    assert schema_validate("start_planning_session", {"anything": True}) == Accepted()


# --------------------------------------------------------------------------- #
# dependencies_batch                                                          #
# --------------------------------------------------------------------------- #


def test_dependencies_batch_detects_cycle() -> None:
    result = validate_dependencies_batch([_edge("A", "B"), _edge("B", "A")], [])
    assert isinstance(result, Denied)
    assert result.code == "cycle_detected"
    assert result.blockers == ["B -> A -> B"]


def test_dependencies_batch_indirect_cycle_through_existing() -> None:
    existing = [_edge("A", "B"), _edge("B", "C")]
    result = validate_dependencies_batch([_edge("C", "A")], existing)
    assert isinstance(result, Denied)
    assert result.code == "cycle_detected"
    assert result.blockers == ["C -> A -> B -> C"]


def test_dependencies_batch_fully_deduped() -> None:
    existing = [_edge("x", "y")]
    result = validate_dependencies_batch([_edge("x", "y"), _edge("x", "y")], existing)
    assert isinstance(result, Denied)
    assert result.code == "batch_fully_deduped"


def test_dependencies_batch_accepts_new_acyclic_edges() -> None:
    result = validate_dependencies_batch([_edge("a", "b"), _edge("c", "d")], [])
    assert result == Accepted()


def test_run_dependencies_batch_reports_to_persist_and_drops() -> None:
    # a→b ok; duplicate a→b dropped; c→d ok.
    result = run_dependencies_batch([_edge("a", "b"), _edge("a", "b"), _edge("c", "d")], [])
    from specsmither.lifecycle.prechecks import BatchValidationOk

    assert isinstance(result, BatchValidationOk)
    assert result.to_persist == [_edge("a", "b"), _edge("c", "d")]
    assert result.dropped_intra_batch == [_edge("a", "b")]


# --------------------------------------------------------------------------- #
# gate_currently_passing — ALWAYS re-validates (no TTL trust)                  #
# --------------------------------------------------------------------------- #


def _session() -> PlanningSession:
    return PlanningSession(
        specification_id="spec-1",
        current_phase=PlanningPhase.CROSS_VALIDATION.value,
        last_gate_result="pass",
        last_validated_at="2026-06-26T00:00:00Z",
    )


def test_gate_currently_passing_revalidates_to_fail_even_if_cache_says_pass() -> None:
    # Session cache says 'pass' and is "fresh", but the validator re-runs and fails.
    validator = _FakeValidator(
        _output("fail", [ValidatorFinding(category="rubric", message="Epic e1 too thin", severity="denial")])
    )
    result = gate_currently_passing(_session(), _spec_full(), validator, CONFIG)
    assert validator.calls == 1  # ALWAYS re-validates — no TTL trust
    assert isinstance(result, Denied)
    assert result.code == "gate_not_passed"
    assert result.blockers == ["Epic e1 too thin"]


def test_gate_currently_passing_revalidates_to_pass() -> None:
    validator = _FakeValidator(_output("pass"))
    result = gate_currently_passing(_session(), _spec_full(), validator, CONFIG)
    assert validator.calls == 1
    assert result == Accepted()


def test_gate_currently_passing_no_spec_full_denied() -> None:
    validator = _FakeValidator(_output("pass"))
    result = gate_currently_passing(_session(), None, validator, CONFIG)
    assert validator.calls == 0
    assert isinstance(result, Denied)
    assert result.code == "gate_not_passed"


# --------------------------------------------------------------------------- #
# gate_currently_passing routes through the PHASE-GATE evaluator, not the      #
# validator's composite gate_result (simulator find 2026-07-13).              #
# The ``*_expansion`` phases gate on spec-wide per-entity all-pass and EXCLUDE #
# the global cascade (topology / dependency DAG), which is enforced only at    #
# cross_validation. Otherwise ticket_expansion can never complete while the    #
# tickets are still islands (global score 0 before the DAG is wired).          #
# --------------------------------------------------------------------------- #

_THRESH_CONFIG: dict[str, Any] = {"thresholds": {"specification": 80, "epic": 70, "ticket": 70}}


def _expansion_session() -> PlanningSession:
    return PlanningSession(
        specification_id="spec-1",
        current_phase=PlanningPhase.TICKET_EXPANSION.value,
        last_gate_result="fail",
        last_validated_at="2026-07-13T00:00:00Z",
    )


def _te_output(gate_result: str, per_ticket_score: dict[str, float]) -> ValidatorOutput:
    return ValidatorOutput(
        gate_result=gate_result,  # type: ignore[arg-type]
        local_score=0.0,
        per_epic_score={},
        per_ticket_score=per_ticket_score,
        findings=[],
        validated_phase=PlanningPhase.TICKET_EXPANSION,
    )


def test_ticket_expansion_gate_passes_on_all_pass_despite_cascade_fail() -> None:
    # Every ticket clears the threshold, but the validator's composite gate_result
    # is 'fail' because the global cascade (topology penalty: tickets are islands,
    # no dependency DAG yet) drives the global score below 80. The phase gate for
    # ticket_expansion must IGNORE the cascade → the phase completes; the DAG is
    # wired later at cross_validation.
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1"), _ticket("t2", "e1")])])
    validator = _FakeValidator(_te_output("fail", {"t1": 100.0, "t2": 100.0}))
    result = gate_currently_passing(_expansion_session(), full, validator, _THRESH_CONFIG)
    assert result == Accepted()


def test_ticket_expansion_gate_fails_when_a_ticket_is_below_threshold() -> None:
    # Spec-wide all-pass: one ticket short → the phase gate fails regardless of the
    # (here 'pass') composite gate_result.
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1"), _ticket("t2", "e1")])])
    validator = _FakeValidator(_te_output("pass", {"t1": 100.0, "t2": 50.0}))
    result = gate_currently_passing(_expansion_session(), full, validator, _THRESH_CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "gate_not_passed"


def test_cross_validation_gate_still_enforces_the_cascade() -> None:
    # By contrast, at cross_validation the gate DEFERS to the composite gate_result,
    # so a failing cascade (the DAG check) still blocks the close.
    session = PlanningSession(
        specification_id="spec-1",
        current_phase=PlanningPhase.CROSS_VALIDATION.value,
        last_gate_result="pass",
        last_validated_at="2026-07-13T00:00:00Z",
    )
    full = _spec_full(epics=[_epic("e1", [_ticket("t1", "e1")])])
    validator = _FakeValidator(_output("fail"))
    result = gate_currently_passing(session, full, validator, _THRESH_CONFIG)
    assert isinstance(result, Denied)
    assert result.code == "gate_not_passed"
