"""Tests for the pure :mod:`specsmither.adapters.in_memory_operations` projector.

Three guarantees, mirroring the work-item #17 contract:

1. **Projection** — each op produces the expected post-mutation
   :class:`crucible.models.Specification`.
2. **No mutation leak** — the input spec is a deep clone; the original is never
   touched by a projection.
3. **Projector-matches-persisted** — for the same op, the in-memory projection's
   ``model_dump(by_alias=True, exclude_none=True)`` equals the dump of the spec
   recomposed from rows after the op is applied through the SQLite stores. Both
   sides are derived from one DB round-trip (the baseline is read back from the
   database), so only the op's own effect can differ — count/tree/validator-cache
   columns are recompute-owned and never surface in ``build_spec_full``, so they
   are not part of the compared nested view.
"""

from __future__ import annotations

from pathlib import Path

from crucible.models import (
    AcceptanceCriterion,
    ImplementationStep,
    Specification,
    Ticket,
)
from crucible.models.enums import BlueprintCategory, Complexity, DependencyType, TicketType
from crucible.models.enums import (
    TestType as _TestType,  # aliased: bare `TestType` trips pytest class collection
)

from specsmither.adapters.in_memory_operations import (
    CreateBlueprint,
    CreateDependencies,
    CreateEpic,
    CreateTicket,
    DeleteBlueprint,
    DeleteDependencies,
    DeleteEpic,
    DeleteTicket,
    DependencyEdgeSpec,
    LinkBlueprintToTickets,
    OperationsProjector,
    UnlinkBlueprintFromTickets,
    UpdateEpic,
    UpdateSpec,
    UpdateTicket,
    apply_mutation,
    project_dependency_edges,
)
from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.repositories import AllStores, ProjectRecord, make_stores
from specsmither.domain.enums import SpecStatus, TicketStatus
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
    build_spec_full,
)

PROJECT_ID = "proj-1"
SPEC_ID = "spec-1"
EPIC_ID = "epic-1"
BP_ID = "bp-1"


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _records() -> tuple[
    SpecificationRecord,
    list[EpicRecord],
    list[TicketRecord],
    list[BlueprintRecord],
    list[DependencyEdge],
]:
    spec = SpecificationRecord(
        id=SPEC_ID, project_id=PROJECT_ID, title="Demo", status=SpecStatus.PLANNING
    )
    epic = EpicRecord(
        id=EPIC_ID,
        specification_id=SPEC_ID,
        title="Foundation",
        description="Lay the foundation.",
        objective="Establish the base.",
        epic_number=1,
        order=0,
    )
    ticket_a = TicketRecord(
        id="ticket-a",
        epic_id=EPIC_ID,
        title="First ticket",
        ticket_number=1,
        order=0,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.SMALL,
        estimated_minutes=30,
        status=TicketStatus.READY,
        acceptance_criteria=[
            AcceptanceCriterion(id="ac-1", given="g", when="w", then="t", order=0)
        ],
        implementation_steps=[ImplementationStep(id="step-1", text="do it", order=0)],
        files_to_be_created=["src/a.py"],
        test_types=[_TestType.UNIT],
        quality_gates=["ruff"],
    )
    ticket_b = TicketRecord(
        id="ticket-b",
        epic_id=EPIC_ID,
        title="Second ticket",
        ticket_number=2,
        order=1,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.MEDIUM,
        estimated_minutes=60,
        status=TicketStatus.PENDING,
    )
    blueprint = BlueprintRecord(
        id=BP_ID,
        specification_id=SPEC_ID,
        title="System diagram",
        category=BlueprintCategory.ARCHITECTURE,
        content="graph TD; A-->B",
        order=0,
    )
    return spec, [epic], [ticket_a, ticket_b], [blueprint], []


def _spec_full() -> Specification:
    spec, epics, tickets, blueprints, deps = _records()
    return build_spec_full(spec, epics, tickets, blueprints, deps)


def _dump(spec: Specification) -> dict[str, object]:
    return spec.model_dump(by_alias=True, exclude_none=True)


def _ticket(spec: Specification, ticket_id: str) -> Ticket:
    return next(t for e in spec.epics for t in e.tickets if t.id == ticket_id)


# --------------------------------------------------------------------------- #
# 1. projection + no mutation leak                                            #
# --------------------------------------------------------------------------- #


def test_update_ticket_projects_and_does_not_leak() -> None:
    original = _spec_full()
    before = _dump(original)

    projected = apply_mutation(original, UpdateTicket(id="ticket-a", fields={"title": "Renamed"}))

    # (a) the projection reflects the change ...
    assert _ticket(projected, "ticket-a").title == "Renamed"
    # ... and leaves the rest of the ticket intact (deep clone preserves children).
    assert len(_ticket(projected, "ticket-a").acceptance_criteria) == 1
    assert _ticket(projected, "ticket-a").implementation_steps[0].text == "do it"

    # (b) the ORIGINAL is untouched (no shared mutable state).
    assert _ticket(original, "ticket-a").title == "First ticket"
    assert _dump(original) == before
    assert projected is not original


def test_update_spec_and_epic_fields() -> None:
    original = _spec_full()

    projected = apply_mutation(original, UpdateSpec(fields={"title": "New title", "background": "ctx"}))
    assert projected.title == "New title"
    assert projected.background == "ctx"
    assert original.title == "Demo"

    projected_epic = apply_mutation(original, UpdateEpic(id=EPIC_ID, fields={"objective": "Q"}))
    assert projected_epic.epics[0].objective == "Q"
    assert original.epics[0].objective == "Establish the base."


def test_create_and_delete_epic() -> None:
    original = _spec_full()

    created = apply_mutation(original, CreateEpic(title="Second epic"))
    assert len(created.epics) == 2
    new_epic = created.epics[-1]
    assert new_epic.id == "__proj_epic_2"
    assert new_epic.specification_id == SPEC_ID
    assert new_epic.title == "Second epic"
    assert len(original.epics) == 1  # no leak

    created_explicit = apply_mutation(original, CreateEpic(title="E", id="epic-explicit"))
    assert created_explicit.epics[-1].id == "epic-explicit"

    deleted = apply_mutation(original, DeleteEpic(id=EPIC_ID))
    assert deleted.epics == []
    assert len(original.epics) == 1


def test_create_and_delete_ticket() -> None:
    original = _spec_full()

    created = apply_mutation(original, CreateTicket(epic_id=EPIC_ID, title="Third ticket"))
    tickets = created.epics[0].tickets
    assert [t.id for t in tickets] == ["ticket-a", "ticket-b", "__proj_ticket_3"]
    assert tickets[-1].epic_id == EPIC_ID
    assert tickets[-1].ticket_type == TicketType.IMPLEMENTATION
    assert tickets[-1].complexity == Complexity.MEDIUM
    assert len(original.epics[0].tickets) == 2  # no leak

    deleted = apply_mutation(original, DeleteTicket(id="ticket-b"))
    assert [t.id for t in deleted.epics[0].tickets] == ["ticket-a"]
    assert len(original.epics[0].tickets) == 2


def test_create_and_delete_blueprint() -> None:
    original = _spec_full()

    created = apply_mutation(
        original, CreateBlueprint(title="ERD", category=BlueprintCategory.ERD)
    )
    assert len(created.blueprints) == 2
    assert created.blueprints[-1].id == "__proj_bp_2"
    assert created.blueprints[-1].category == BlueprintCategory.ERD
    assert len(original.blueprints) == 1  # no leak

    deleted = apply_mutation(original, DeleteBlueprint(id=BP_ID))
    assert deleted.blueprints == []
    assert len(original.blueprints) == 1


def test_link_and_unlink_blueprint() -> None:
    original = _spec_full()

    linked = apply_mutation(
        original, LinkBlueprintToTickets(blueprint_id=BP_ID, ticket_ids=["ticket-a"])
    )
    refs = _ticket(linked, "ticket-a").blueprint_references
    assert [r.blueprint_id for r in refs] == [BP_ID]
    assert _ticket(original, "ticket-a").blueprint_references == []  # no leak

    # Linking is idempotent (no duplicate reference).
    relinked = apply_mutation(
        linked, LinkBlueprintToTickets(blueprint_id=BP_ID, ticket_ids=["ticket-a"])
    )
    assert len(_ticket(relinked, "ticket-a").blueprint_references) == 1

    unlinked = apply_mutation(
        linked, UnlinkBlueprintFromTickets(blueprint_id=BP_ID, ticket_ids=["ticket-a"])
    )
    assert _ticket(unlinked, "ticket-a").blueprint_references == []


def test_add_and_remove_dependency_projection() -> None:
    original = _spec_full()
    edge = DependencyEdgeSpec(from_ticket_id="ticket-b", to_ticket_id="ticket-a")

    added = apply_mutation(original, CreateDependencies(dependencies=[edge]))
    links = _ticket(added, "ticket-b").dependencies
    assert [(link.ticket_id, link.type) for link in links] == [
        ("ticket-a", DependencyType.REQUIRES)
    ]
    assert _ticket(original, "ticket-b").dependencies == []  # no leak

    # create is dedup'd on the (from, to) pair.
    again = apply_mutation(added, CreateDependencies(dependencies=[edge]))
    assert len(_ticket(again, "ticket-b").dependencies) == 1

    removed = apply_mutation(added, DeleteDependencies(dependencies=[edge]))
    assert _ticket(removed, "ticket-b").dependencies == []


def test_project_dependency_edges_is_inverse_of_build() -> None:
    spec = apply_mutation(
        _spec_full(),
        CreateDependencies(
            dependencies=[DependencyEdgeSpec(from_ticket_id="ticket-b", to_ticket_id="ticket-a")]
        ),
    )
    edges = project_dependency_edges(spec)
    assert [(e.ticket_id, e.depends_on_id, e.type) for e in edges] == [
        ("ticket-b", "ticket-a", DependencyType.REQUIRES)
    ]


def test_projector_class_and_function_agree() -> None:
    original = _spec_full()
    op = UpdateTicket(id="ticket-a", fields={"title": "X"})
    assert _dump(OperationsProjector().apply(original, op)) == _dump(apply_mutation(original, op))


# --------------------------------------------------------------------------- #
# 3. projector-matches-persisted invariant (over a real SQLite database)      #
# --------------------------------------------------------------------------- #


def _seed_db(stores: AllStores) -> None:
    spec, epics, tickets, blueprints, _deps = _records()
    stores.projects.create_project(ProjectRecord(id=PROJECT_ID, name="P"))
    stores.specifications.create_specification(spec)
    for epic in epics:
        stores.epics.create_epic(epic)
    for ticket in tickets:
        stores.tickets.create_ticket(ticket)
    for blueprint in blueprints:
        stores.blueprints.create_blueprint(blueprint)


def _load_spec_full(stores: AllStores) -> Specification:
    spec = stores.specifications.get_specification(SPEC_ID)
    epics = stores.epics.list_epics(specification_id=SPEC_ID)
    tickets: list[TicketRecord] = []
    for epic in epics:
        tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
    blueprints = stores.blueprints.list_blueprints(specification_id=SPEC_ID)
    deps = stores.ticket_dependencies.list_dependencies(specification_id=SPEC_ID)
    return build_spec_full(spec, epics, tickets, blueprints, deps)


def test_projector_matches_persisted_add_dependency(tmp_path: Path) -> None:
    factory = make_session_factory(init_db(tmp_path / "proj.db"))

    with factory.begin() as session:
        _seed_db(make_stores(session))

    # Baseline read back from the database (so any round-trip quirk is shared).
    with factory() as session:
        baseline = _load_spec_full(make_stores(session))

    op = CreateDependencies(
        dependencies=[DependencyEdgeSpec(from_ticket_id="ticket-b", to_ticket_id="ticket-a")]
    )
    projected = apply_mutation(baseline, op)

    # Apply the same op through the stores, then recompose from rows.
    with factory.begin() as session:
        make_stores(session).ticket_dependencies.add_dependency(
            ticket_id="ticket-b", depends_on_id="ticket-a", type=DependencyType.REQUIRES
        )
    with factory() as session:
        persisted = _load_spec_full(make_stores(session))

    assert _dump(projected) == _dump(persisted)


def test_projector_matches_persisted_update_ticket(tmp_path: Path) -> None:
    factory = make_session_factory(init_db(tmp_path / "proj.db"))

    with factory.begin() as session:
        _seed_db(make_stores(session))
    with factory() as session:
        baseline = _load_spec_full(make_stores(session))

    projected = apply_mutation(baseline, UpdateTicket(id="ticket-a", fields={"title": "Renamed"}))

    with factory.begin() as session:
        stores = make_stores(session)
        record = stores.tickets.get_ticket("ticket-a")
        assert record is not None
        stores.tickets.update_ticket(record.model_copy(update={"title": "Renamed"}))
    with factory() as session:
        persisted = _load_spec_full(make_stores(session))

    assert _dump(projected) == _dump(persisted)
