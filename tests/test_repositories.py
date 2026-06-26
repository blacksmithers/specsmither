"""L5 acceptance: the ``*StoreSqlite`` repository layer over a real SQLite database.

Drives the stores (bound per transaction through :func:`make_stores`) against an
on-disk ``tmp_path`` database (``init_db`` + ``make_session_factory``), inside
genuine mutation transactions, and asserts the five repository invariants:

1. **Ticket decompose/recompose round-trip** — a hydrated :class:`TicketRecord`
   fans out into the four owned child tables on write and recomposes equal on read;
   ``update_ticket`` is REPLACE-ALL (old child rows are gone).
2. **Delta-mutators are no-ops** — ``create_ticket`` (and the ``updateX*Count``
   family) never write a count column; those are the recompute worklist's (#14).
3. **Work-session per-ticket lock** — a second live session for the same ticket
   hits the partial-unique lock (``IntegrityError`` → a lock ``CrudError``); closing
   the first releases it.
4. **Planning datapoint idempotency** — re-upserting the same composite id updates
   in place (exactly one row, last score wins).
5. **Not-found seam** — ``get_ticket`` returns ``None`` for a missing id, while the
   ``require_found``-backed gets (project / work-session) raise ``NotFoundError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from crucible.models import AcceptanceCriterion, CodeReference, ImplementationStep
from crucible.models.enums import BlueprintCategory, Complexity, TicketType
from crucible.models.enums import (
    TestType as _TestType,  # aliased: bare `TestType` trips pytest class collection
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import AcceptanceCriterion as ACRow
from specsmither.db.models import Epic as EpicModel
from specsmither.db.models import PlanningSession
from specsmither.db.models import Specification as SpecModel
from specsmither.db.models import Ticket as TicketModel
from specsmither.db.repositories import AllStores, ProjectRecord, make_stores
from specsmither.domain.enums import TicketStatus, WorkSessionStatus
from specsmither.domain.records import (
    BlueprintRecord,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
)
from specsmither.operations.errors import (
    ConflictError,
    CrudErrorCode,
    NotFoundError,
    PreconditionFailedError,
)

PROJECT_ID = "proj-1"
SPEC_ID = "spec-1"
EPIC_ID = "epic-1"
TICKET_ID = "ticket-1"


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    """A session factory over a freshly bootstrapped on-disk database."""
    engine = init_db(tmp_path / "repos.db")
    return make_session_factory(engine)


def _seed(stores: AllStores) -> None:
    """Create the project → spec → epic spine the tickets/work/planning rows hang off."""
    stores.projects.create_project(ProjectRecord(id=PROJECT_ID, name="P"))
    stores.specifications.create_specification(
        SpecificationRecord(id=SPEC_ID, project_id=PROJECT_ID, title="S")
    )
    stores.epics.create_epic(
        EpicRecord(
            id=EPIC_ID,
            specification_id=SPEC_ID,
            title="E",
            description="d",
            objective="o",
            epic_number=1,
            order=0,
        )
    )


def _full_ticket() -> TicketRecord:
    """A fully-hydrated ticket: 2 ACs, 2 steps, two file groups, 2 test types, a code ref."""
    return TicketRecord(
        id=TICKET_ID,
        epic_id=EPIC_ID,
        title="T",
        ticket_number=1,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.MEDIUM,
        estimated_minutes=30,
        status=TicketStatus.PENDING,
        acceptance_criteria=[
            AcceptanceCriterion(id="ac-1", given="g1", when="w1", then="t1", order=0),
            AcceptanceCriterion(id="ac-2", given="g2", when="w2", then="t2", order=1),
        ],
        implementation_steps=[
            ImplementationStep(id="step-1", text="step one", order=0),
            ImplementationStep(id="step-2", text="step two", order=1),
        ],
        files_to_be_created=["src/a.py", "src/b.py"],
        files_to_be_modified=["src/c.py"],
        test_types=[_TestType.UNIT, _TestType.INTEGRATION],
        code_references=[CodeReference(file_path="src/ref.py", symbol="foo")],
    )


# --------------------------------------------------------------------------- #
# 1. ticket decompose / recompose round-trip + REPLACE-ALL update             #
# --------------------------------------------------------------------------- #


def test_ticket_decompose_recompose_round_trip(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    with factory.begin() as session:
        stores = make_stores(session)
        _seed(stores)
        stores.tickets.create_ticket(_full_ticket())
        # A blueprint hung off the same spec exercises the BlueprintStoreSqlite codec.
        stores.blueprints.create_blueprint(
            BlueprintRecord(
                id="bp-1",
                specification_id=SPEC_ID,
                title="BP",
                category=BlueprintCategory.ARCHITECTURE,
                content="graph TD; A-->B",
            )
        )

    # Fresh session: every child-backed array must recompose EQUAL.
    with factory() as session:
        stores = make_stores(session)
        got = stores.tickets.get_ticket(TICKET_ID)
        assert got is not None

        assert len(got.acceptance_criteria) == 2
        assert [ac.given for ac in got.acceptance_criteria] == ["g1", "g2"]
        assert [ac.when for ac in got.acceptance_criteria] == ["w1", "w2"]
        assert [ac.then for ac in got.acceptance_criteria] == ["t1", "t2"]

        assert len(got.implementation_steps) == 2
        assert [s.text for s in got.implementation_steps] == ["step one", "step two"]

        # File groups land in the right `kind` bucket.
        assert got.files_to_be_created == ["src/a.py", "src/b.py"]
        assert got.files_to_be_modified == ["src/c.py"]
        assert got.files_to_be_deleted == []
        assert got.files_to_be_referenced == []

        assert list(got.test_types) == [_TestType.UNIT, _TestType.INTEGRATION]

        assert len(got.code_references) == 1
        assert got.code_references[0].file_path == "src/ref.py"

        blueprint = stores.blueprints.get_blueprint("bp-1")
        assert blueprint.category == BlueprintCategory.ARCHITECTURE
        assert blueprint.content == "graph TD; A-->B"

    # update_ticket is REPLACE-ALL: shrink to 1 AC and 0 impl steps.
    with factory.begin() as session:
        stores = make_stores(session)
        current = stores.tickets.get_ticket(TICKET_ID)
        assert current is not None
        shrunk = current.model_copy(
            update={
                "acceptance_criteria": [
                    AcceptanceCriterion(id="ac-1", given="g1", when="w1", then="t1", order=0)
                ],
                "implementation_steps": [],
            }
        )
        stores.tickets.update_ticket(shrunk)

    with factory() as session:
        stores = make_stores(session)
        got = stores.tickets.get_ticket(TICKET_ID)
        assert got is not None
        assert len(got.acceptance_criteria) == 1
        assert len(got.implementation_steps) == 0
        # The old child rows are physically gone (REPLACE-ALL, not merge).
        ac_rows = session.scalar(
            select(func.count()).select_from(ACRow).where(ACRow.ticket_id == TICKET_ID)
        )
        assert ac_rows == 1


# --------------------------------------------------------------------------- #
# 2. delta-mutators are no-ops (counts owned by recompute #14)                 #
# --------------------------------------------------------------------------- #


def test_delta_mutators_are_noops(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    with factory.begin() as session:
        stores = make_stores(session)
        _seed(stores)
        stores.tickets.create_ticket(TicketRecord(id=TICKET_ID, epic_id=EPIC_ID, title="T"))

    # Creating a ticket must NOT have bumped any rollup count.
    with factory() as session:
        epic = session.get(EpicModel, EPIC_ID)
        spec = session.get(SpecModel, SPEC_ID)
        assert epic is not None and spec is not None
        assert epic.ticket_count == 0
        assert spec.ticket_count == 0
        assert spec.epic_count == 0

    # Calling the delta-mutators explicitly is a no-op — nothing moves.
    with factory.begin() as session:
        stores = make_stores(session)
        stores.tickets.update_ticket_incoming_dep_count(TICKET_ID, 5)
        stores.tickets.update_ticket_blueprint_count(TICKET_ID, 5)
        stores.epics.update_epic_ticket_count(EPIC_ID, 3)
        stores.specifications.update_specification_ticket_count(SPEC_ID, 3)
        stores.specifications.update_specification_epic_count(SPEC_ID, 3)
        stores.projects.update_project_ticket_count(PROJECT_ID, 3)

    with factory() as session:
        epic = session.get(EpicModel, EPIC_ID)
        spec = session.get(SpecModel, SPEC_ID)
        ticket = session.get(TicketModel, TICKET_ID)
        assert epic is not None and spec is not None and ticket is not None
        assert epic.ticket_count == 0
        assert spec.ticket_count == 0
        assert spec.epic_count == 0
        assert ticket.incoming_dep_count == 0
        assert ticket.blueprint_count == 0


# --------------------------------------------------------------------------- #
# 3. work-session per-ticket lock                                             #
# --------------------------------------------------------------------------- #


def test_work_session_per_ticket_lock(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    with factory.begin() as session:
        stores = make_stores(session)
        _seed(stores)
        stores.tickets.create_ticket(TicketRecord(id=TICKET_ID, epic_id=EPIC_ID, title="T"))

    # Open one live session and round-trip a couple of child rows.
    with factory.begin() as session:
        stores = make_stores(session)
        ws = stores.work_sessions.create_work_session(TICKET_ID)
        assert ws.status == WorkSessionStatus.ACTIVE
        ws_id = ws.id

    with factory.begin() as session:
        stores = make_stores(session)
        stores.work_acceptance_checks.upsert_check(ws_id, "ac-1", checked=True)
        stores.work_test_results.append_test_result(
            ws_id, "unit", passed=3, failed=0, all_passed=True
        )

    with factory() as session:
        stores = make_stores(session)
        checks = stores.work_acceptance_checks.list_by_session(ws_id)
        assert len(checks) == 1
        assert checks[0].checked is True
        results = stores.work_test_results.list_by_session(ws_id)
        assert len(results) == 1
        assert results[0].all_passed is True

    # A second live session for the SAME ticket collides on the partial-unique lock.
    # The lock surfaces as a CrudError; intent="lock" maps it to PRECONDITION_FAILED
    # (ConflictError is the sibling lock outcome the contract also permits).
    with (
        pytest.raises((ConflictError, PreconditionFailedError)) as excinfo,
        factory.begin() as session,
    ):
        stores = make_stores(session)
        stores.work_sessions.create_work_session(TICKET_ID)
    assert excinfo.value.code in {
        CrudErrorCode.CONFLICT,
        CrudErrorCode.PRECONDITION_FAILED,
    }

    # Closing the first session releases the lock → a new live session is allowed.
    with factory.begin() as session:
        stores = make_stores(session)
        stores.work_sessions.close_work_session(ws_id)

    with factory.begin() as session:
        stores = make_stores(session)
        ws2 = stores.work_sessions.create_work_session(TICKET_ID)
        assert ws2.status == WorkSessionStatus.ACTIVE
        assert ws2.id != ws_id


# --------------------------------------------------------------------------- #
# 4. planning datapoint idempotency                                          #
# --------------------------------------------------------------------------- #


def test_planning_datapoint_upsert_is_idempotent(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    with factory.begin() as session:
        stores = make_stores(session)
        _seed(stores)
        # Planning sessions are created via the WritePlan executor (not a store) —
        # insert the parent row directly to satisfy the datapoint's FK.
        session.add(PlanningSession(id="ps-1", specification_id=SPEC_ID))

    with factory.begin() as session:
        stores = make_stores(session)
        stores.planning_datapoints.upsert_datapoint(
            planning_session_id="ps-1",
            trigger_action_id="act1",
            entity_id="ent1",
            trigger="created",
            score=0.5,
        )

    # Re-upsert the same composite id with a different score.
    with factory.begin() as session:
        stores = make_stores(session)
        stores.planning_datapoints.upsert_datapoint(
            planning_session_id="ps-1",
            trigger_action_id="act1",
            entity_id="ent1",
            trigger="created",
            score=0.9,
        )

    with factory() as session:
        stores = make_stores(session)
        points = stores.planning_datapoints.list_datapoints("ps-1")
        assert len(points) == 1
        assert points[0].id == "act1#ent1#created"
        assert points[0].score == 0.9


# --------------------------------------------------------------------------- #
# 5. not-found seam                                                           #
# --------------------------------------------------------------------------- #


def test_not_found_paths(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    with factory() as session:
        stores = make_stores(session)
        # get_ticket permits absence: a missing id returns None (no raise).
        assert stores.tickets.get_ticket("missing") is None
        # require_found-backed gets demand existence → NotFoundError.
        with pytest.raises(NotFoundError):
            stores.projects.get_project("missing")
        with pytest.raises(NotFoundError):
            stores.work_sessions.get_work_session("missing")
