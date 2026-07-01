"""The coupled lifecycle-ports seam (``adapters/lifecycle_ports.py``).

Drives the concrete ports against a real on-disk SQLite database (``tmp_path`` via
``init_db`` + ``make_session_factory``) seeded with a project → spec → epic → 2
tickets + one dependency, and asserts the three seam contracts:

1. :meth:`SqliteSpecStore.get_spec_full` recomposes a :class:`SpecFull` whose
   ``.spec`` is a crucible :class:`~crucible.models.Specification` that scores
   through the real crucible validator, and whose flat ``epics`` / tickets /
   ``dependencies`` side-projections are populated (the ``B requires A`` edge
   present, with ``from`` / ``to`` mapped correctly).
2. :meth:`ProjectorOperationsLayer.apply_mutation` projects an ``update_ticket`` in
   memory — returning a NEW :class:`SpecFull` reflecting the change while leaving
   both the input snapshot and the database untouched.
3. :func:`make_lifecycle_ports` binds all eight :class:`LifecyclePorts` slots over
   one session.

Topology: project → spec (``status='planning'``) → 1 epic → tickets A, B; ``B``
depends on ``A``.
"""

from __future__ import annotations

from pathlib import Path

import crucible
from crucible.models import Specification as CrucibleSpecification
from sqlalchemy.orm import Session, sessionmaker

from specsmither.adapters.crucible_validator import CrucibleValidatorAdapter
from specsmither.adapters.lifecycle_ports import (
    ProjectorOperationsLayer,
    SqlitePlanningSessionStore,
    SqliteSpecStore,
    make_lifecycle_ports,
)
from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    Epic,
    Project,
    Specification,
    Ticket,
    TicketDependency,
)
from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.ports import (
    BlueprintRef,
    EpicFull,
    SpecDependencyEdge,
    SpecFull,
    TicketRef,
)

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
EPIC_ID = "01EPIC000000000000000000A"
TICKET_A = "01TICKETA000000000000000A"
TICKET_B = "01TICKETB000000000000000B"


def _open(tmp_path: Path) -> sessionmaker[Session]:
    engine = init_db(tmp_path / "lifecycle_ports.db")
    return make_session_factory(engine)


def _seed(session: Session) -> None:
    session.add(Project(id=PROJECT_ID, name="P"))
    session.add(
        Specification(
            id=SPEC_ID,
            project_id=PROJECT_ID,
            title="S",
            status="planning",
            specification_type_id=None,
        )
    )
    session.add(
        Epic(
            id=EPIC_ID,
            specification_id=SPEC_ID,
            epic_number=1,
            title="E",
            description="d",
            objective="o",
            order=1,
        )
    )
    session.add(
        Ticket(id=TICKET_A, epic_id=EPIC_ID, title="A", ticket_number=1, order=1)
    )
    session.add(
        Ticket(id=TICKET_B, epic_id=EPIC_ID, title="B", ticket_number=2, order=2)
    )
    session.flush()
    # B depends on A → directed edge ticket_id=B, depends_on_id=A.
    session.add(TicketDependency(ticket_id=TICKET_B, depends_on_id=TICKET_A))


def _find_ticket(spec_full: SpecFull, ticket_id: str) -> TicketRef:
    for epic in spec_full.epics:
        for ticket in epic.tickets:
            if ticket.id == ticket_id:
                return ticket
    raise AssertionError(f"ticket {ticket_id} not found in flat view")


def test_get_spec_full_recomposes_spec_and_flat_views(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)

    with factory.begin() as session:
        store = SqliteSpecStore(session)
        spec_full = store.get_spec_full(SPEC_ID)

        assert spec_full is not None
        # .spec is the nested crucible Specification (the validator's input).
        assert isinstance(spec_full.spec, CrucibleSpecification)
        assert spec_full.spec.id == SPEC_ID
        assert {epic.id for epic in spec_full.spec.epics} == {EPIC_ID}
        assert {t.id for epic in spec_full.spec.epics for t in epic.tickets} == {
            TICKET_A,
            TICKET_B,
        }

        # Flat side-projections are populated.
        assert [epic.id for epic in spec_full.epics] == [EPIC_ID]
        assert isinstance(spec_full.epics[0], EpicFull)
        flat_ticket_ids = {t.id for epic in spec_full.epics for t in epic.tickets}
        assert flat_ticket_ids == {TICKET_A, TICKET_B}

        # The dependency edge is present and directed B (from) → A (to).
        assert spec_full.dependencies == [
            SpecDependencyEdge(from_ticket_id=TICKET_B, to_ticket_id=TICKET_A)
        ]
        # …and survives the recompose onto the nested ticket (B carries the link to A).
        ticket_b = next(
            t for epic in spec_full.spec.epics for t in epic.tickets if t.id == TICKET_B
        )
        assert [link.ticket_id for link in ticket_b.dependencies] == [TICKET_A]

        # The recomposed spec scores through the real crucible validator.
        output = CrucibleValidatorAdapter().validate(
            spec_full, PlanningPhase.PLANNING_SPEC, crucible.load_defaults()
        )
        assert output.gate_result in {"pass", "fail"}
        assert output.validated_phase == PlanningPhase.PLANNING_SPEC


def test_get_spec_full_missing_returns_none(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        assert SqliteSpecStore(session).get_spec_full("01NOSUCHSPEC0000000000000Z") is None


def test_projector_apply_mutation_is_pure(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)

    with factory.begin() as session:
        spec_full = SqliteSpecStore(session).get_spec_full(SPEC_ID)
        assert spec_full is not None

        layer = ProjectorOperationsLayer()
        projected = layer.apply_mutation(
            "update_ticket",
            {"id": TICKET_A, "fields": {"title": "Renamed A"}},
            spec_full,
        )

        # A NEW SpecFull reflecting the change.
        assert projected is not spec_full
        assert _find_ticket(projected, TICKET_A).title == "Renamed A"
        # The input snapshot is untouched (the projector deep-clones).
        assert _find_ticket(spec_full, TICKET_A).title == "A"
        # The projected flat views keep the full topology (B still depends on A).
        assert projected.dependencies == [
            SpecDependencyEdge(from_ticket_id=TICKET_B, to_ticket_id=TICKET_A)
        ]

    # The database was never written — A's title is still "A".
    with factory.begin() as session:
        row = session.get(Ticket, TICKET_A)
        assert row is not None
        assert row.title == "A"


def test_make_lifecycle_ports_binds_all_eight_slots(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        ports = make_lifecycle_ports(session)

        assert isinstance(ports.planning_session_store, SqlitePlanningSessionStore)
        assert isinstance(ports.spec_store, SqliteSpecStore)
        assert isinstance(ports.operations, ProjectorOperationsLayer)
        # The remaining five slots are bound (and non-None: persist / clock / id_gen
        # are wired here even though the dataclass makes them optional).
        assert ports.config_store is not None
        assert ports.validator is not None
        assert ports.persist_write_plan is not None
        assert ports.clock is not None
        assert ports.id_generator is not None

        # clock yields a datetime; id_generator a fresh 26-char ULID.
        assert ports.clock().tzinfo is not None
        assert len(ports.id_generator()) == 26


def test_planning_session_store_missing_returns_none(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        store = SqlitePlanningSessionStore(session)
        # No sessions seeded → every read is empty / None (no NotFoundError leak).
        assert store.get_planning_session("01NOPE0000000000000000000Z") is None
        assert store.get_active_planning_session_by_spec(SPEC_ID) is None
        assert store.list_planning_sessions_by_spec(SPEC_ID) == []
        assert store.list_planning_sessions_by_project(PROJECT_ID) == []


def test_get_spec_returns_childless_specification(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)

    with factory.begin() as session:
        spec = SqliteSpecStore(session).get_spec(SPEC_ID)
        assert spec is not None
        assert isinstance(spec, CrucibleSpecification)
        assert spec.id == SPEC_ID
        assert spec.project_id == PROJECT_ID
        assert spec.status == "planning"
        # The light single-item read carries no children.
        assert spec.epics == []
        assert spec.blueprints == []


def test_blueprint_ref_projection_shape() -> None:
    # Guards the BlueprintRef flat-view contract the spec store emits (no blueprints
    # are seeded above, so assert the type is importable / constructible here).
    ref = BlueprintRef(id="b1", specification_id=SPEC_ID, title="BP", category="architecture")
    assert ref.category == "architecture"
