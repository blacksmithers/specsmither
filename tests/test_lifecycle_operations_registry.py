"""Tests for the planning operations registry (A1 §1.2 port).

Covers the classifier (forbidden / native / late) across representative
operations and phases, plus the load-bearing data: ``create_dependencies``
``max_batch == 5000``, the ``delete_*`` guards, and the (absent) create-side
ratio guard.
"""

from __future__ import annotations

import pytest

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.operations_registry import (
    OPERATIONS,
    PLANNING_MUTATING_OPERATIONS,
    PLANNING_READ_ONLY_OPERATIONS,
    PLANNING_SYNTHETIC_OPERATIONS,
    GuardSpec,
    OperationDef,
    classify_operation_call,
    get_operation_def,
)

P = PlanningPhase


# --------------------------------------------------------------------------- #
# Registry shape / exhaustivity                                                #
# --------------------------------------------------------------------------- #


def test_registry_has_26_operations() -> None:
    assert len(OPERATIONS) == 26
    assert len(PLANNING_MUTATING_OPERATIONS) == 16
    assert len(PLANNING_READ_ONLY_OPERATIONS) == 1
    assert len(PLANNING_SYNTHETIC_OPERATIONS) == 9


def test_non_synthetic_ops_have_defs() -> None:
    for op in (*PLANNING_MUTATING_OPERATIONS, *PLANNING_READ_ONLY_OPERATIONS):
        assert isinstance(OPERATIONS[op], OperationDef)


def test_synthetic_ops_map_to_none() -> None:
    for op in PLANNING_SYNTHETIC_OPERATIONS:
        assert OPERATIONS[op] is None


# --------------------------------------------------------------------------- #
# classify_operation_call — forbidden / native / late                         #
# --------------------------------------------------------------------------- #


def test_update_spec_native_in_planning_spec() -> None:
    assert classify_operation_call("update_spec", P.PLANNING_SPEC) == "native"


def test_update_spec_forbidden_in_later_phases() -> None:
    assert classify_operation_call("update_spec", P.EPIC_EXPANSION) == "forbidden"
    assert classify_operation_call("update_spec", P.EPIC_DECOMPOSITION) == "forbidden"
    assert classify_operation_call("update_spec", P.CROSS_VALIDATION) == "forbidden"
    assert classify_operation_call("update_spec", P.PLANNED) == "forbidden"


def test_create_dependencies_native_in_cross_validation() -> None:
    assert classify_operation_call("create_dependencies", P.CROSS_VALIDATION) == "native"


def test_create_dependencies_forbidden_in_all_earlier_phases() -> None:
    for phase in (
        P.PLANNING_SPEC,
        P.EPIC_DECOMPOSITION,
        P.EPIC_EXPANSION,
        P.TICKET_DECOMPOSITION,
        P.TICKET_EXPANSION,
        P.PLANNED,
    ):
        assert classify_operation_call("create_dependencies", phase) == "forbidden"


def test_late_op_native_phase_op_called_in_a_later_phase() -> None:
    # create_epic is native to epic_decomposition and only forbidden in
    # planning_spec / planned, so calling it in a strictly later phase is 'late'.
    assert classify_operation_call("create_epic", P.EPIC_DECOMPOSITION) == "native"
    assert classify_operation_call("create_epic", P.EPIC_EXPANSION) == "late"
    assert classify_operation_call("create_epic", P.CROSS_VALIDATION) == "late"
    # update_epic is native to epic_expansion; ticket_decomposition is later and
    # not forbidden -> late.
    assert classify_operation_call("update_epic", P.TICKET_DECOMPOSITION) == "late"


def test_create_epic_forbidden_in_planning_spec() -> None:
    assert classify_operation_call("create_epic", P.PLANNING_SPEC) == "forbidden"


def test_get_planning_status_native_in_every_phase() -> None:
    for phase in PlanningPhase:
        assert classify_operation_call("get_planning_status", phase) == "native"


def test_classify_raises_for_synthetic_ops() -> None:
    # Synthetic op names are valid PlanningOperationName values, but have no def.
    with pytest.raises(ValueError, match="audit-only"):
        classify_operation_call("start_planning_session", P.PLANNING_SPEC)
    with pytest.raises(ValueError, match="audit-only"):
        classify_operation_call("phase_advance", P.EPIC_DECOMPOSITION)
    with pytest.raises(ValueError, match="audit-only"):
        classify_operation_call("human_approve", P.CROSS_VALIDATION)
    with pytest.raises(ValueError, match="audit-only"):
        classify_operation_call("session_closed", P.PLANNED)


# --------------------------------------------------------------------------- #
# Load-bearing data: max_batch + guards                                       #
# --------------------------------------------------------------------------- #


def test_create_dependencies_max_batch_is_5000() -> None:
    spec = get_operation_def("create_dependencies")
    assert spec is not None
    assert spec.max_batch == 5000


def test_delete_epic_has_min_count_guard() -> None:
    spec = get_operation_def("delete_epic")
    assert spec is not None
    assert GuardSpec in {type(g) for g in spec.guards}
    assert [g.type for g in spec.guards] == ["minCount"]


def test_delete_ticket_has_min_count_guard() -> None:
    spec = get_operation_def("delete_ticket")
    assert spec is not None
    assert [g.type for g in spec.guards] == ["minCount"]


def test_delete_blueprint_has_ratio_guard() -> None:
    spec = get_operation_def("delete_blueprint")
    assert spec is not None
    assert [g.type for g in spec.guards] == ["blueprintEpicRatio"]


def test_create_blueprint_has_no_ratio_guard() -> None:
    spec = get_operation_def("create_blueprint")
    assert spec is not None
    assert spec.guards == ()
    assert "blueprintEpicRatio" not in {g.type for g in spec.guards}


# --------------------------------------------------------------------------- #
# get_operation_def                                                            #
# --------------------------------------------------------------------------- #


def test_get_operation_def_returns_def_for_real_op() -> None:
    spec = get_operation_def("update_spec")
    assert isinstance(spec, OperationDef)
    assert spec.name == "update_spec"
    assert spec.native_phase == P.PLANNING_SPEC


def test_get_operation_def_returns_none_for_synthetic_op() -> None:
    assert get_operation_def("phase_advance") is None
    assert get_operation_def("human_reject_with_feedback") is None


def test_get_planning_status_is_the_only_multi_actor_op() -> None:
    multi = [
        op
        for op, spec in OPERATIONS.items()
        if spec is not None and spec.multi_actor
    ]
    assert multi == ["get_planning_status"]


def test_native_phases_band_blueprint_link_from_ticket_decomposition() -> None:
    # MB.9 — blueprint-link/unlink are native across ticket_decomposition → cross_validation
    # (never late/rollback there), and forbidden in every phase before ticket_decomposition.
    for op in ("link_blueprint_to_tickets", "unlink_blueprint_to_tickets"):
        assert classify_operation_call(op, P.EPIC_EXPANSION) == "forbidden"
        for phase in (P.TICKET_DECOMPOSITION, P.TICKET_EXPANSION, P.CROSS_VALIDATION):
            assert classify_operation_call(op, phase) == "native"
