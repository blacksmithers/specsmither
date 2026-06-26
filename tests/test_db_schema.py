"""L2 acceptance: the schema bootstraps, cascades, and enforces its locks.

Exercises the persistence layer end-to-end against real on-disk SQLite files
(``tmp_path``) through ``init_db`` / ``make_session_factory``:

1. ``init_db`` builds every model table and seeds exactly one default
   ``specification_type`` (and re-running migrations is a no-op).
2. ``ON DELETE CASCADE`` tears the whole ``specification → epic → ticket → child``
   subtree down via a *Core* delete — proving ``PRAGMA foreign_keys=ON`` plus the
   ``ondelete="CASCADE"`` foreign keys are wired (a Core delete bypasses the ORM
   unit-of-work cascade, so it can only succeed via the DB cascade).
3. The planning-session partial-unique lock rejects a 2nd active session per spec
   but allows a ``closed`` one.
4. The work-session partial-unique lock does the same per ticket.
5. ``ticket_dependencies`` rejects a duplicate ``(ticket_id, depends_on_id)`` edge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from specsmither.db.base import make_session_factory, new_ulid
from specsmither.db.migrations import (
    BASELINE_VERSION,
    apply_migrations,
    current_version,
    get_default_specification_type_id,
    init_db,
)
from specsmither.db.models import (
    AcceptanceCriterion,
    Base,
    Blueprint,
    Epic,
    ImplementationStep,
    PlanningSession,
    Project,
    Specification,
    SpecificationType,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
    TicketFileChange,
    TicketTest,
    WorkSession,
)
from specsmither.domain.enums import FileChangeKind

# The 26 ORM model tables apply_migrations must create. Spelled out explicitly
# rather than read from Base.metadata: sibling test modules register throwaway
# tables (json_probe / counter) on the same shared registry, so Base.metadata is
# not a clean source of truth once the whole suite is imported.
EXPECTED_MODEL_TABLES = frozenset(
    {
        # core entities + git + spec-type
        "projects",
        "specification_types",
        "specifications",
        "epics",
        "tickets",
        "blueprints",
        "ticket_dependencies",
        "ticket_blueprint_refs",
        "specification_prs",
        "epic_commits",
        # ticket child-backed arrays
        "acceptance_criteria",
        "implementation_steps",
        "ticket_file_changes",
        "ticket_tests",
        "code_snippets",
        "type_snippets",
        # planning-session
        "planning_sessions",
        "planning_session_actions",
        "planning_phase_transitions",
        "planning_entity_score_datapoints",
        "planning_session_aggregates",
        # work-session
        "work_sessions",
        "work_session_acceptance_checks",
        "work_session_impl_step_completions",
        "work_session_file_changes",
        "work_session_test_results",
    }
)


def _count(session: Session, model: type[Base]) -> int:
    """Return the row count of *model*'s table."""
    return session.execute(select(sa.func.count()).select_from(model)).scalar_one()


def _seed_spec(session: Session) -> tuple[str, str]:
    """Insert a minimal ``project → specification`` and return ``(project_id, spec_id)``."""
    project_id, spec_id = new_ulid(), new_ulid()
    session.add(Project(id=project_id, name="P"))
    session.add(Specification(id=spec_id, project_id=project_id, title="S"))
    return project_id, spec_id


def _seed_ticket(session: Session) -> tuple[str, str, str, str]:
    """Insert ``project → spec → epic → ticket`` and return all four ids."""
    project_id, spec_id = _seed_spec(session)
    epic_id, ticket_id = new_ulid(), new_ulid()
    session.add(
        Epic(
            id=epic_id,
            specification_id=spec_id,
            epic_number=1,
            title="E",
            description="d",
            objective="o",
        )
    )
    session.add(Ticket(id=ticket_id, epic_id=epic_id, title="T"))
    return project_id, spec_id, epic_id, ticket_id


def test_init_db_creates_all_tables_and_seeds_single_default(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "schema.db")
    try:
        names = set(inspect(engine).get_table_names())

        # Every ORM model table is present (superset check) plus the version ledger.
        assert names >= EXPECTED_MODEL_TABLES
        assert "schema_migrations" in names
        assert len(EXPECTED_MODEL_TABLES) == 26
        assert current_version(engine) == BASELINE_VERSION == 1

        factory = make_session_factory(engine)
        with factory() as session:
            default_id = get_default_specification_type_id(session)
            assert isinstance(default_id, str) and default_id
            n_default = session.execute(
                select(sa.func.count())
                .select_from(SpecificationType)
                .where(SpecificationType.is_default.is_(True))
            ).scalar_one()
            assert n_default == 1

        # Re-running migrations is a no-op: still version 1, still one default.
        apply_migrations(engine)
        assert current_version(engine) == 1
        with factory() as session:
            assert _count(session, SpecificationType) == 1
    finally:
        engine.dispose()


def test_cascade_delete_tears_down_full_spec_subtree(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "cascade.db")
    factory = make_session_factory(engine)
    try:
        project_id, spec_id = new_ulid(), new_ulid()
        epic_id = new_ulid()
        ticket_id, ticket2_id = new_ulid(), new_ulid()
        blueprint_id = new_ulid()

        with factory.begin() as session:
            session.add(Project(id=project_id, name="P"))
            session.add(Specification(id=spec_id, project_id=project_id, title="S"))
            session.add(
                Epic(
                    id=epic_id,
                    specification_id=spec_id,
                    epic_number=1,
                    title="E",
                    description="d",
                    objective="o",
                )
            )
            session.add(Ticket(id=ticket_id, epic_id=epic_id, title="T1"))
            session.add(Ticket(id=ticket2_id, epic_id=epic_id, title="T2"))
            session.add(
                Blueprint(
                    id=blueprint_id,
                    specification_id=spec_id,
                    category="design",
                    title="BP",
                    content="# design",
                )
            )
            # The ticket child-array tables carry no parent relationship() (repos do
            # replace-all), so the unit of work cannot order them after their ticket
            # from raw FK values alone. Flush the spine first — still one transaction.
            session.flush()

            session.add(
                AcceptanceCriterion(
                    ticket_id=ticket_id, given="g", when="w", then="t", order=0
                )
            )
            session.add(ImplementationStep(ticket_id=ticket_id, text="do it", order=0))
            session.add(
                TicketFileChange(
                    ticket_id=ticket_id,
                    path="src/x.py",
                    kind=FileChangeKind.TO_BE_CREATED.value,
                    order=0,
                )
            )
            session.add(TicketTest(ticket_id=ticket_id, test_type="unit", order=0))
            session.add(
                TicketDependency(ticket_id=ticket_id, depends_on_id=ticket2_id)
            )
            session.add(
                TicketBlueprintRef(ticket_id=ticket_id, blueprint_id=blueprint_id)
            )

        # Sanity: the whole subtree is present before the delete.
        with factory() as session:
            assert _count(session, Ticket) == 2
            assert _count(session, AcceptanceCriterion) == 1
            assert _count(session, TicketDependency) == 1
            assert _count(session, TicketBlueprintRef) == 1

        # Core delete of the spec: only the DB FK cascade can remove descendants.
        with factory.begin() as session:
            session.execute(sa.delete(Specification).where(Specification.id == spec_id))

        with factory() as session:
            assert _count(session, Specification) == 0
            assert _count(session, Epic) == 0
            assert _count(session, Ticket) == 0
            assert _count(session, AcceptanceCriterion) == 0
            assert _count(session, ImplementationStep) == 0
            assert _count(session, TicketFileChange) == 0
            assert _count(session, TicketTest) == 0
            assert _count(session, TicketDependency) == 0
            assert _count(session, Blueprint) == 0
            assert _count(session, TicketBlueprintRef) == 0
            # The parent project is untouched by the spec delete.
            assert _count(session, Project) == 1
    finally:
        engine.dispose()


def test_planning_session_one_active_per_spec_lock(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "planning_lock.db")
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            _, spec_id = _seed_spec(session)

        with factory.begin() as session:
            session.add(PlanningSession(id=new_ulid(), specification_id=spec_id))

        # A 2nd active session for the same spec violates the partial-unique lock.
        with (
            pytest.raises(IntegrityError),
            factory.begin() as session,
        ):
            session.add(PlanningSession(id=new_ulid(), specification_id=spec_id))

        # A closed session sits outside the index predicate — allowed.
        with factory.begin() as session:
            session.add(
                PlanningSession(
                    id=new_ulid(), specification_id=spec_id, status="closed"
                )
            )

        with factory() as session:
            assert _count(session, PlanningSession) == 2
    finally:
        engine.dispose()


def test_work_session_one_active_per_ticket_lock(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "work_lock.db")
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            _, _, _, ticket_id = _seed_ticket(session)

        with factory.begin() as session:
            session.add(WorkSession(id=new_ulid(), ticket_id=ticket_id))

        # A 2nd active work session on the same ticket violates the lock.
        with (
            pytest.raises(IntegrityError),
            factory.begin() as session,
        ):
            session.add(WorkSession(id=new_ulid(), ticket_id=ticket_id))

        # A closed work session is permitted.
        with factory.begin() as session:
            session.add(
                WorkSession(id=new_ulid(), ticket_id=ticket_id, status="closed")
            )

        with factory() as session:
            assert _count(session, WorkSession) == 2
    finally:
        engine.dispose()


def test_ticket_dependency_unique_edge(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "dep_unique.db")
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            _, _, epic_id, ticket_id = _seed_ticket(session)
            ticket2_id = new_ulid()
            session.add(Ticket(id=ticket2_id, epic_id=epic_id, title="T2"))

        with factory.begin() as session:
            session.add(
                TicketDependency(ticket_id=ticket_id, depends_on_id=ticket2_id)
            )

        # The same directed edge a second time hits UNIQUE(ticket_id, depends_on_id).
        with (
            pytest.raises(IntegrityError),
            factory.begin() as session,
        ):
            session.add(
                TicketDependency(ticket_id=ticket_id, depends_on_id=ticket2_id)
            )

        with factory() as session:
            assert _count(session, TicketDependency) == 1
    finally:
        engine.dispose()
