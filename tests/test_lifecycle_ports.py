"""Tests for the lifecycle seam contracts (``specsmither.lifecycle.ports``).

The ports module is pure — Protocols + dataclasses, no concrete impls — so these
tests assert two things: (1) the data contracts (:class:`ValidatorOutput`,
:class:`SpecFull` and friends) construct and expose the documented fields, built
from a real crucible :class:`~crucible.models.Specification` via
:func:`specsmither.domain.records.build_spec_full`; and (2) a trivial stub class
structurally satisfies each Protocol (the seam is implementable).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from crucible.models import Specification
from crucible.models.enums import BlueprintCategory, DependencyType

from specsmither.domain.enums import FindingCategory, PlanningPhase
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
    build_spec_full,
)
from specsmither.lifecycle.ports import (
    BlueprintRef,
    ConfigStore,
    EpicFull,
    LifecyclePorts,
    OperationsLayer,
    PlanningSessionStore,
    ProjectConfigEntry,
    SpecConfigEntry,
    SpecDependencyEdge,
    SpecFull,
    SpecStore,
    TicketRef,
    Validator,
    ValidatorFinding,
    ValidatorOutput,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _build_specification() -> Specification:
    """Recompose a minimal two-ticket spec via the M0 ``build_spec_full``."""

    spec = SpecificationRecord(id="spec-1", project_id="proj-1", title="Demo spec")
    epic = EpicRecord(
        id="epic-1",
        specification_id="spec-1",
        title="Epic one",
        description="An epic.",
        objective="Ship it.",
    )
    tickets = [
        TicketRecord(id="tk-1", epic_id="epic-1", title="Ticket one"),
        TicketRecord(id="tk-2", epic_id="epic-1", title="Ticket two"),
    ]
    blueprint = BlueprintRecord(
        id="bp-1",
        specification_id="spec-1",
        title="Pattern",
        category=BlueprintCategory.ARCHITECTURE,
    )
    deps = [DependencyEdge(ticket_id="tk-2", depends_on_id="tk-1", type=DependencyType.REQUIRES)]
    return build_spec_full(spec, [epic], tickets, [blueprint], deps)


def _build_spec_full() -> SpecFull:
    """Wrap the recomposed spec into the :class:`SpecFull` read contract."""

    specification = _build_specification()
    return SpecFull(
        spec=specification,
        epics=[
            EpicFull(
                id="epic-1",
                specification_id="spec-1",
                title="Epic one",
                tickets=[
                    TicketRef(id="tk-1", epic_id="epic-1", title="Ticket one", status="pending"),
                    TicketRef(id="tk-2", epic_id="epic-1", title="Ticket two", status="pending"),
                ],
            )
        ],
        blueprints=[
            BlueprintRef(
                id="bp-1", specification_id="spec-1", title="Pattern", category="pattern"
            )
        ],
        dependencies=[SpecDependencyEdge(from_ticket_id="tk-2", to_ticket_id="tk-1")],
    )


# --------------------------------------------------------------------------- #
# Data contracts                                                              #
# --------------------------------------------------------------------------- #


def test_validator_output_field_access() -> None:
    finding = ValidatorFinding(
        category=FindingCategory.RUBRIC,
        message="Score below threshold.",
        severity="finding",
        entity_id="epic-1",
        entity_type="epic",
        points_lost=0.2,
    )
    output = ValidatorOutput(
        gate_result="pass",
        local_score=0.91,
        per_epic_score={"epic-1": 0.88},
        per_ticket_score={"tk-1": 0.95, "tk-2": 0.9},
        findings=[finding],
        validated_phase=PlanningPhase.EPIC_EXPANSION,
    )

    assert output.gate_result == "pass"
    assert output.local_score == 0.91
    assert output.per_epic_score["epic-1"] == 0.88
    assert output.per_ticket_score["tk-2"] == 0.9
    assert output.validated_phase is PlanningPhase.EPIC_EXPANSION
    assert output.findings[0].severity == "finding"
    assert output.findings[0].entity_id == "epic-1"
    assert output.findings[0].path is None
    assert output.findings[0].points_lost == 0.2


def test_validator_finding_category_accepts_enum_or_str() -> None:
    enum_finding = ValidatorFinding(
        category=FindingCategory.CASCADE, message="m", severity="denial"
    )
    str_finding = ValidatorFinding(category="custom", message="m", severity="denial")
    assert enum_finding.category is FindingCategory.CASCADE
    assert str_finding.category == "custom"


def test_spec_full_field_access() -> None:
    spec_full = _build_spec_full()

    # `spec` is the nested crucible Specification (the validator's scoring input).
    assert isinstance(spec_full.spec, Specification)
    assert spec_full.spec.id == "spec-1"
    assert len(spec_full.spec.epics) == 1
    assert len(spec_full.spec.epics[0].tickets) == 2

    # Flat side-projections.
    assert spec_full.epics[0].id == "epic-1"
    assert spec_full.epics[0].specification_id == "spec-1"
    assert spec_full.epics[0].tickets[1].id == "tk-2"
    assert spec_full.epics[0].tickets[0].status == "pending"
    assert spec_full.blueprints[0].category == "pattern"
    assert spec_full.dependencies[0].from_ticket_id == "tk-2"
    assert spec_full.dependencies[0].to_ticket_id == "tk-1"


def test_spec_full_dependencies_default_empty() -> None:
    spec_full = SpecFull(spec=_build_specification(), epics=[], blueprints=[])
    assert spec_full.dependencies == []


def test_config_entries_field_access() -> None:
    project_entry = ProjectConfigEntry(domain="planning", overrides={"a": 1}, schema_version=2)
    spec_entry = SpecConfigEntry(domain="planning", snapshot={"b": 2}, schema_version=1)
    assert project_entry.domain == "planning"
    assert project_entry.overrides == {"a": 1}
    assert spec_entry.snapshot == {"b": 2}
    assert spec_entry.schema_version == 1


# --------------------------------------------------------------------------- #
# Protocol structural conformance                                             #
# --------------------------------------------------------------------------- #


class _StubSessionStore:
    def get_planning_session(self, session_id: str) -> Any:
        return None

    def get_active_planning_session_by_spec(self, spec_id: str) -> Any:
        return None

    def list_planning_sessions_by_spec(self, spec_id: str) -> list[Any]:
        return []

    def list_planning_sessions_by_project(self, project_id: str) -> list[Any]:
        return []


class _StubSpecStore:
    def get_spec(self, spec_id: str) -> Specification | None:
        return None

    def get_spec_full(self, spec_id: str) -> SpecFull | None:
        return None


class _StubConfigStore:
    def get_project_overrides(self, project_id: str, domain: str) -> Any | None:
        return None

    def set_project_overrides(
        self, project_id: str, domain: str, overrides: Any, schema_version: int
    ) -> None:
        return None

    def list_project_configs(self, project_id: str) -> list[ProjectConfigEntry]:
        return []

    def get_spec_snapshot(self, spec_id: str, domain: str) -> Any | None:
        return None

    def set_spec_snapshot(
        self, spec_id: str, domain: str, snapshot: Any, schema_version: int
    ) -> None:
        return None

    def list_spec_configs(self, spec_id: str) -> list[SpecConfigEntry]:
        return []


class _StubValidator:
    def validate(
        self, spec_full: SpecFull, phase: PlanningPhase, config: Mapping[str, Any]
    ) -> ValidatorOutput:
        return ValidatorOutput(
            gate_result="pass",
            local_score=1.0,
            per_epic_score={},
            per_ticket_score={},
            findings=[],
            validated_phase=phase,
        )


class _StubOperations:
    def apply_mutation(
        self, op: str, payload: Mapping[str, Any], spec_full: SpecFull
    ) -> SpecFull:
        return spec_full


def test_stubs_satisfy_protocols() -> None:
    assert isinstance(_StubSessionStore(), PlanningSessionStore)
    assert isinstance(_StubSpecStore(), SpecStore)
    assert isinstance(_StubConfigStore(), ConfigStore)
    assert isinstance(_StubValidator(), Validator)
    assert isinstance(_StubOperations(), OperationsLayer)


def test_lifecycle_ports_bundle_constructs() -> None:
    ports = LifecyclePorts(
        planning_session_store=_StubSessionStore(),
        spec_store=_StubSpecStore(),
        config_store=_StubConfigStore(),
        validator=_StubValidator(),
        operations=_StubOperations(),
    )
    # Optional ports default to None.
    assert ports.persist_write_plan is None
    assert ports.clock is None
    assert ports.id_generator is None

    # The bundle accepts injected clock / id_generator callables.
    wired = LifecyclePorts(
        planning_session_store=_StubSessionStore(),
        spec_store=_StubSpecStore(),
        config_store=_StubConfigStore(),
        validator=_StubValidator(),
        operations=_StubOperations(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        id_generator=lambda: "01J0",
    )
    assert wired.clock is not None
    assert wired.clock().year == 2026
    assert wired.id_generator is not None
    assert wired.id_generator() == "01J0"


def test_validator_stub_round_trips_through_protocol() -> None:
    validator: Validator = _StubValidator()
    out = validator.validate(_build_spec_full(), PlanningPhase.PLANNING_SPEC, {})
    assert out.gate_result == "pass"
    assert out.validated_phase is PlanningPhase.PLANNING_SPEC
