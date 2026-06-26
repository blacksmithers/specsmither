"""Unit tests for the runtime record DTOs and the ``build_spec_full`` recompose."""

from __future__ import annotations

import crucible
from crucible.models import (
    AcceptanceCriterion,
    ImplementationStep,
    Specification,
)
from crucible.models.enums import (
    BlueprintCategory,
    Complexity,
    DependencyType,
    TicketType,
)
from crucible.models.enums import (
    TestType as _TestType,  # aliased: bare `TestType` trips pytest class collection
)

from specsmither.domain.enums import EpicStatus, SpecStatus, TicketStatus
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketBlueprintRef,
    TicketRecord,
    build_spec_full,
)


def _fixture() -> tuple[
    SpecificationRecord,
    list[EpicRecord],
    list[TicketRecord],
    list[BlueprintRecord],
    list[DependencyEdge],
    list[TicketBlueprintRef],
]:
    spec = SpecificationRecord(
        id="spec-1",
        project_id="proj-1",
        title="Demo spec",
        status=SpecStatus.PLANNING,
    )
    epic = EpicRecord(
        id="epic-1",
        specification_id="spec-1",
        title="Foundation",
        description="Lay the foundation.",
        objective="Establish the base.",
        order=0,
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
            AcceptanceCriterion(
                id="ac-1",
                given="a fresh repo",
                when="the foundation is built",
                then="the base exists",
                order=0,
            )
        ],
        implementation_steps=[ImplementationStep(id="step-1", text="Create the base.", order=0)],
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
    blueprint = BlueprintRecord(
        id="bp-1",
        specification_id="spec-1",
        title="System diagram",
        category=BlueprintCategory.ARCHITECTURE,
        content="graph TD; A-->B",
        order=0,
    )
    # ticket-b depends on ticket-a.
    deps = [DependencyEdge(ticket_id="ticket-b", depends_on_id="ticket-a", type=DependencyType.REQUIRES)]
    refs = [TicketBlueprintRef(ticket_id="ticket-a", blueprint_id="bp-1", context="overview")]
    return spec, [epic], [ticket_a, ticket_b], [blueprint], deps, refs


def test_build_spec_full_wires_nested_children() -> None:
    spec, epics, tickets, blueprints, deps, refs = _fixture()

    result = build_spec_full(spec, epics, tickets, blueprints, deps, blueprint_refs=refs)

    assert isinstance(result, Specification)
    assert result.id == "spec-1"
    assert result.project_id == "proj-1"

    # One epic carrying its two tickets, ordered by `order` then `id`.
    assert len(result.epics) == 1
    epic = result.epics[0]
    assert epic.id == "epic-1"
    assert [t.id for t in epic.tickets] == ["ticket-a", "ticket-b"]

    # Dependency edge mapped onto the dependent ticket as a DependencyLink whose
    # `ticket_id` is the depended-on ticket.
    ticket_b = epic.tickets[1]
    assert len(ticket_b.dependencies) == 1
    assert ticket_b.dependencies[0].ticket_id == "ticket-a"
    assert ticket_b.dependencies[0].type == DependencyType.REQUIRES

    # ticket-a has no dependencies.
    assert epic.tickets[0].dependencies == []


def test_build_spec_full_ticket_children_and_blueprints() -> None:
    spec, epics, tickets, blueprints, deps, refs = _fixture()

    result = build_spec_full(spec, epics, tickets, blueprints, deps, blueprint_refs=refs)
    ticket_a = result.epics[0].tickets[0]

    assert len(ticket_a.acceptance_criteria) == 1
    assert ticket_a.acceptance_criteria[0].given == "a fresh repo"
    assert len(ticket_a.implementation_steps) == 1
    assert ticket_a.files_to_be_created == ["src/base.py"]

    # testSpecification emitted only because there is >=1 planned test type.
    assert ticket_a.test_specification is not None
    assert ticket_a.test_specification.test_types == [_TestType.UNIT]

    # ticket-b has no planned tests -> no testSpecification.
    assert result.epics[0].tickets[1].test_specification is None

    # blueprint_refs wired onto the linked ticket.
    assert len(ticket_a.blueprint_references) == 1
    assert ticket_a.blueprint_references[0].blueprint_id == "bp-1"

    # Spec carries the blueprint.
    assert len(result.blueprints) == 1
    assert result.blueprints[0].id == "bp-1"
    assert result.blueprints[0].category == BlueprintCategory.ARCHITECTURE


def test_build_spec_full_dumps_to_valid_crucible_input() -> None:
    spec, epics, tickets, blueprints, deps, refs = _fixture()

    result = build_spec_full(spec, epics, tickets, blueprints, deps, blueprint_refs=refs)
    dumped = result.model_dump(by_alias=True, exclude_none=True)

    # Recomposed dict round-trips through crucible without raising.
    structural = crucible.validate_structural(dumped)
    assert structural is not None

    full = crucible.validate(dumped)
    assert full is not None
