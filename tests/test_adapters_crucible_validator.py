"""Tests for the crucible-backed :class:`Validator` adapter (work item #5).

Parity coverage for ``adapters/crucible_validator.py`` (port of
``lifecycle/adapters/validator-adapter.ts``, A1 §3.2): the ``planned``
short-circuit (which must NOT touch crucible), the ``activeEntityId`` scoping
(``getActiveEntityId``), the binary-phase gate/score mapping, and the
per-entity-score routing onto ``per_epic_score`` / ``per_ticket_score``.
"""

from __future__ import annotations

from typing import Any

import crucible
import pytest
from crucible.models import AcceptanceCriterion, ImplementationStep
from crucible.models.enums import Complexity, TicketType
from crucible.models.enums import (
    TestType as _TestType,  # aliased: bare `TestType` trips pytest class collection
)

from specsmither.adapters import crucible_validator as adapter_module
from specsmither.adapters.crucible_validator import (
    CrucibleValidatorAdapter,
    _get_active_entity_id,
)
from specsmither.domain.enums import (
    EpicStatus,
    FindingCategory,
    PlanningPhase,
    SpecStatus,
    TicketStatus,
)
from specsmither.domain.records import (
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
    build_spec_full,
)
from specsmither.lifecycle.ports import EpicFull, SpecFull, TicketRef, ValidatorOutput


def _spec_full() -> SpecFull:
    """A small two-epic / three-ticket :class:`SpecFull` for the adapter probes."""
    spec = SpecificationRecord(
        id="spec-1",
        project_id="proj-1",
        title="Demo spec",
        status=SpecStatus.PLANNING,
    )
    epic_1 = EpicRecord(
        id="epic-1",
        specification_id="spec-1",
        title="Foundation",
        description="Lay the foundation.",
        objective="Establish the base.",
        order=0,
        status=EpicStatus.TODO,
    )
    epic_2 = EpicRecord(
        id="epic-2",
        specification_id="spec-1",
        title="Surface",
        description="Build the surface.",
        objective="Expose the API.",
        order=1,
        status=EpicStatus.TODO,
    )
    ticket_a = TicketRecord(
        id="ticket-a",
        epic_id="epic-1",
        title="First ticket",
        order=0,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.SMALL,
        estimated_minutes=30,
        status=TicketStatus.READY,
        acceptance_criteria=[
            AcceptanceCriterion(id="ac-1", given="g", when="w", then="t", order=0)
        ],
        implementation_steps=[ImplementationStep(id="step-1", text="Create.", order=0)],
        files_to_be_created=["src/base.py"],
        test_types=[_TestType.UNIT],
        quality_gates=["ruff"],
    )
    ticket_b = TicketRecord(
        id="ticket-b",
        epic_id="epic-1",
        title="Second ticket",
        order=1,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.MEDIUM,
        estimated_minutes=60,
        status=TicketStatus.PENDING,
    )
    ticket_c = TicketRecord(
        id="ticket-c",
        epic_id="epic-2",
        title="Third ticket",
        order=0,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.MEDIUM,
        estimated_minutes=45,
        status=TicketStatus.PENDING,
    )
    nested = build_spec_full(
        spec, [epic_1, epic_2], [ticket_a, ticket_b, ticket_c], [], []
    )
    return SpecFull(
        spec=nested,
        epics=[
            EpicFull(
                id="epic-1",
                specification_id="spec-1",
                title="Foundation",
                tickets=[
                    TicketRef(id="ticket-a", epic_id="epic-1", title="First ticket"),
                    TicketRef(id="ticket-b", epic_id="epic-1", title="Second ticket"),
                ],
            ),
            EpicFull(
                id="epic-2",
                specification_id="spec-1",
                title="Surface",
                tickets=[
                    TicketRef(id="ticket-c", epic_id="epic-2", title="Third ticket"),
                ],
            ),
        ],
        blueprints=[],
    )


def _config() -> dict[str, Any]:
    return crucible.load_defaults()


# --------------------------------------------------------------------------- #
# activeEntityId scoping (getActiveEntityId)                                   #
# --------------------------------------------------------------------------- #


def test_active_entity_id_epic_expansion_is_all_epic_ids() -> None:
    assert _get_active_entity_id(_spec_full(), PlanningPhase.EPIC_EXPANSION) == [
        "epic-1",
        "epic-2",
    ]


def test_active_entity_id_ticket_expansion_flattens_ticket_ids() -> None:
    assert _get_active_entity_id(_spec_full(), PlanningPhase.TICKET_EXPANSION) == [
        "ticket-a",
        "ticket-b",
        "ticket-c",
    ]


def test_active_entity_id_is_none_for_ticket_decomposition() -> None:
    # ticket_decomposition is spec-wide — no active entity (A1 §3.2, M8.5 finding).
    assert _get_active_entity_id(_spec_full(), PlanningPhase.TICKET_DECOMPOSITION) is None


@pytest.mark.parametrize(
    "phase",
    [PlanningPhase.PLANNING_SPEC, PlanningPhase.EPIC_DECOMPOSITION, PlanningPhase.CROSS_VALIDATION],
)
def test_active_entity_id_is_none_for_non_score_phases(phase: PlanningPhase) -> None:
    assert _get_active_entity_id(_spec_full(), phase) is None


# --------------------------------------------------------------------------- #
# 'planned' (and any non-validator phase) short-circuits WITHOUT crucible      #
# --------------------------------------------------------------------------- #


def test_planned_short_circuits_to_trivial_pass_without_calling_crucible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("crucible.validate must not be called for 'planned'")

    monkeypatch.setattr(adapter_module, "crucible_validate", _explode)

    out = CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.PLANNED, _config()
    )

    assert isinstance(out, ValidatorOutput)
    assert out.gate_result == "pass"
    assert out.local_score == 1.0
    assert out.per_epic_score == {}
    assert out.per_ticket_score == {}
    assert out.findings == []
    assert out.validated_phase == PlanningPhase.PLANNED


# --------------------------------------------------------------------------- #
# Binary phase → pass/fail gate + numeric score + findings                     #
# --------------------------------------------------------------------------- #


def test_binary_phase_returns_gate_score_and_findings() -> None:
    out = CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.PLANNING_SPEC, _config()
    )

    assert isinstance(out, ValidatorOutput)
    assert out.gate_result in {"pass", "fail"}
    assert isinstance(out.local_score, float)
    assert isinstance(out.findings, list)
    # A bare spec under-scores at planning_spec → fail + advisory findings.
    assert out.gate_result == "fail"
    assert out.findings  # non-empty
    assert all(f.severity == "finding" for f in out.findings)
    # Binary phases never populate the per-entity score maps.
    assert out.per_epic_score == {}
    assert out.per_ticket_score == {}
    assert out.validated_phase == PlanningPhase.PLANNING_SPEC


def test_skipped_scoring_phase_maps_to_fail_zero() -> None:
    # ticket_decomposition has no rubric → crucible scoring is `skipped`; the
    # adapter folds that to 'fail' / 0.0 (mirrors `scoring && !scoring.skipped`).
    out = CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.TICKET_DECOMPOSITION, _config()
    )

    assert out.gate_result == "fail"
    assert out.local_score == 0.0
    assert out.per_epic_score == {}
    assert out.per_ticket_score == {}


# --------------------------------------------------------------------------- #
# Per-entity score routing: epic_expansion → epics, ticket_expansion → tickets #
# --------------------------------------------------------------------------- #


def test_epic_expansion_populates_per_epic_score_only() -> None:
    out = CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.EPIC_EXPANSION, _config()
    )

    assert out.per_epic_score  # non-empty
    assert set(out.per_epic_score) == {"epic-1", "epic-2"}
    assert all(isinstance(v, float) for v in out.per_epic_score.values())
    assert out.per_ticket_score == {}
    assert out.validated_phase == PlanningPhase.EPIC_EXPANSION


def test_ticket_expansion_populates_per_ticket_score_only() -> None:
    out = CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.TICKET_EXPANSION, _config()
    )

    assert out.per_ticket_score  # non-empty
    assert set(out.per_ticket_score) == {"ticket-a", "ticket-b", "ticket-c"}
    assert all(isinstance(v, float) for v in out.per_ticket_score.values())
    assert out.per_epic_score == {}
    assert out.validated_phase == PlanningPhase.TICKET_EXPANSION
    # Structural count findings map to the '/structural/<field>' locator + 'count'
    # category (each epic is below the minimum-ticket threshold).
    structural = [f for f in out.findings if (f.path or "").startswith("/structural/")]
    assert structural
    assert all(f.category == FindingCategory.COUNT for f in structural)


# --------------------------------------------------------------------------- #
# activeEntityId is threaded into crucible's context (None for decomposition)  #
# --------------------------------------------------------------------------- #


def test_active_entity_id_threaded_into_crucible_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_validate = crucible.validate
    captured: dict[str, Any] = {}

    def _spy(spec_dict: Any, context: Any) -> Any:
        captured["context"] = context
        return real_validate(spec_dict, context)

    monkeypatch.setattr(adapter_module, "crucible_validate", _spy)

    CrucibleValidatorAdapter().validate(
        _spec_full(), PlanningPhase.TICKET_DECOMPOSITION, _config()
    )

    ctx = captured["context"]
    assert ctx["activeEntityId"] is None
    assert ctx["phase"] == PlanningPhase.TICKET_DECOMPOSITION
    assert ctx["returns"] == ["structural", "scoring", "guidance"]
