"""L7 acceptance: the CRUD mutation primitives (#18) drive recompute end-to-end.

Every test runs against a real on-disk SQLite database (``tmp_path`` via ``init_db``
+ ``make_session_factory``) and exercises the public CRUD verbs — proving the
in-transaction recompute worklist ran on the #18 path itself (not only via the
executor): denormalized counts, half-up progress, the cascaded statuses, the
project spec-buckets, and the cached dependency tree all update inside the verb's
own ``Session.begin()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory, new_ulid
from specsmither.db.migrations import init_db
from specsmither.db.models import Epic, Project, Specification, Ticket, TicketDependency
from specsmither.operations.crud import (
    DependencyRequest,
    add_dependency,
    bulk_add_dependencies,
    create_epic,
    create_specification,
    create_ticket,
    delete_ticket,
    update_specification,
    update_ticket,
    would_create_cycle,
)
from specsmither.operations.errors import (
    ConflictError,
    PreconditionFailedError,
    ValidationFailedError,
)
from specsmither.operations.pull_request import link_pull_request
from specsmither.operations.reopen import reopen_specification


def _factory(tmp_path: Path, name: str = "crud.db") -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / name))


def _seed_project(factory: sessionmaker[Session]) -> str:
    project_id = new_ulid()
    with factory.begin() as session:
        session.add(Project(id=project_id, name="P"))
    return project_id


# --------------------------------------------------------------------------- #
# create + update -> counts / progress / tree                                  #
# --------------------------------------------------------------------------- #


def test_create_ticket_then_done_recomputes_end_to_end(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")
    epic = create_epic(factory, specification_id=spec.id, title="E")
    t1 = create_ticket(factory, epic_id=epic.id, title="T1", fields={"estimated_minutes": 10})
    create_ticket(factory, epic_id=epic.id, title="T2", fields={"estimated_minutes": 20})

    # Mark T1 done through the CRUD update verb (decompose replace-all + recompute).
    done = update_ticket(factory, t1.id, {"status": "done"})
    assert done.status == "done"

    with factory() as session:
        epic_row = session.get(Epic, epic.id)
        assert epic_row is not None
        assert epic_row.ticket_count == 2
        assert epic_row.completed_ticket_count == 1
        assert epic_row.ready_ticket_count == 1
        assert epic_row.progress == 50  # 1 of 2, half-up
        assert epic_row.estimated_minutes == 30

        spec_row = session.get(Specification, spec.id)
        assert spec_row is not None
        assert spec_row.ticket_count == 2
        assert spec_row.completed_ticket_count == 1
        assert spec_row.progress == 50
        # Dependency tree materialised in the same transaction as the mutation.
        assert spec_row.dependency_tree is not None
        assert spec_row.dependency_tree_version >= 1

        project_row = session.get(Project, project_id)
        assert project_row is not None
        assert project_row.ticket_count == 2
        assert project_row.completed_ticket_count == 1
        assert project_row.draft_spec_count == 1


def test_progress_half_up_two_of_three(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")
    epic = create_epic(factory, specification_id=spec.id, title="E")
    tickets = [create_ticket(factory, epic_id=epic.id, title=f"T{i}") for i in range(3)]

    update_ticket(factory, tickets[0].id, {"status": "done"})
    update_ticket(factory, tickets[1].id, {"status": "done"})

    with factory() as session:
        epic_row = session.get(Epic, epic.id)
        spec_row = session.get(Specification, spec.id)
        assert epic_row is not None and spec_row is not None
        assert epic_row.progress == 67  # floor(2/3*100 + 0.5)
        assert spec_row.progress == 67


def test_delete_ticket_drops_counts_and_keeps_siblings(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")
    epic = create_epic(factory, specification_id=spec.id, title="E")
    t1 = create_ticket(factory, epic_id=epic.id, title="T1")
    t2 = create_ticket(factory, epic_id=epic.id, title="T2")
    t3 = create_ticket(factory, epic_id=epic.id, title="T3")

    result = delete_ticket(factory, t3.id)
    assert result.deleted_id == t3.id
    assert result.specification_id == spec.id

    with factory() as session:
        assert session.get(Ticket, t3.id) is None
        assert session.get(Ticket, t1.id) is not None
        assert session.get(Ticket, t2.id) is not None

        epic_row = session.get(Epic, epic.id)
        spec_row = session.get(Specification, spec.id)
        assert epic_row is not None and spec_row is not None
        assert epic_row.ticket_count == 2
        assert spec_row.ticket_count == 2
        assert spec_row.dependency_tree is not None  # tree recomputed after the delete


# --------------------------------------------------------------------------- #
# dependencies                                                                 #
# --------------------------------------------------------------------------- #


def test_add_dependency_rejects_self_cross_spec_and_cycle(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)

    spec_a = create_specification(factory, project_id=project_id, title="A")
    epic_a = create_epic(factory, specification_id=spec_a.id, title="EA")
    a1 = create_ticket(factory, epic_id=epic_a.id, title="A1")
    a2 = create_ticket(factory, epic_id=epic_a.id, title="A2")

    spec_b = create_specification(factory, project_id=project_id, title="B")
    epic_b = create_epic(factory, specification_id=spec_b.id, title="EB")
    b1 = create_ticket(factory, epic_id=epic_b.id, title="B1")

    # self-dependency
    with pytest.raises(ValidationFailedError):
        add_dependency(factory, a1.id, a1.id)

    # cross-specification
    with pytest.raises(ValidationFailedError):
        add_dependency(factory, a1.id, b1.id)

    # cycle: A1 requires A2, then A2 requires A1 -> rejected
    add_dependency(factory, a1.id, a2.id)
    with pytest.raises(ConflictError):
        add_dependency(factory, a2.id, a1.id)

    # duplicate is also a conflict
    with pytest.raises(ConflictError):
        add_dependency(factory, a1.id, a2.id)


def test_would_create_cycle_helper(tmp_path: Path) -> None:
    from specsmither.domain.records import DependencyEdge

    edges = [DependencyEdge(ticket_id="A", depends_on_id="B", type="requires")]
    # B already depends on nothing; A->B exists. Adding B->A closes the loop.
    assert would_create_cycle(edges, "B", "A") is True
    # A->A is trivially a cycle.
    assert would_create_cycle(edges, "A", "A") is True
    # C is unrelated.
    assert would_create_cycle(edges, "C", "A") is False


def test_bulk_add_dependencies_caps_and_partial_success(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")
    epic = create_epic(factory, specification_id=spec.id, title="E")
    ta = create_ticket(factory, epic_id=epic.id, title="TA")
    tb = create_ticket(factory, epic_id=epic.id, title="TB")

    # Cap: 101 items rejected outright (checked before any DB work).
    with pytest.raises(ValidationFailedError):
        bulk_add_dependencies(
            factory, [DependencyRequest("x", "y") for _ in range(101)]
        )

    # Partial success: A->B ok, B->A ok (NO cycle pre-check), A->A self fails,
    # duplicate A->B fails — per-item, isolated by savepoint.
    result = bulk_add_dependencies(
        factory,
        [
            DependencyRequest(ta.id, tb.id),
            DependencyRequest(tb.id, ta.id),
            DependencyRequest(ta.id, ta.id),
            DependencyRequest(ta.id, tb.id),
        ],
    )
    assert result.added == 2
    assert len(result.failed) == 2

    with factory() as session:
        pairs = {
            (e.ticket_id, e.depends_on_id)
            for e in session.execute(select(TicketDependency)).scalars()
        }
        # Both directions persisted — proves no cycle pre-check happened.
        assert (ta.id, tb.id) in pairs
        assert (tb.id, ta.id) in pairs


# --------------------------------------------------------------------------- #
# reopen_specification                                                         #
# --------------------------------------------------------------------------- #


def test_reopen_specification_shifts_project_buckets(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")

    # Move the spec to 'ready' so the project ready bucket holds it.
    update_specification(factory, spec.id, {"status": "ready"})
    with factory() as session:
        project_row = session.get(Project, project_id)
        assert project_row is not None
        assert project_row.ready_spec_count == 1
        assert project_row.planning_spec_count == 0

    reopened = reopen_specification(factory, spec.id)
    assert reopened.status == "planning"
    with factory() as session:
        project_row = session.get(Project, project_id)
        assert project_row is not None
        assert project_row.ready_spec_count == 0
        assert project_row.planning_spec_count == 1

    # A non-'ready' spec (now 'planning') cannot be reopened.
    with pytest.raises(PreconditionFailedError):
        reopen_specification(factory, spec.id)


# --------------------------------------------------------------------------- #
# link_pull_request                                                            #
# --------------------------------------------------------------------------- #


def test_link_pull_request_is_idempotent(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    project_id = _seed_project(factory)
    spec = create_specification(factory, project_id=project_id, title="S")

    first = link_pull_request(factory, spec.id, 42, url="https://example/pull/42")
    assert first.already_linked is False
    assert first.pr_number == 42

    second = link_pull_request(factory, spec.id, 42)
    assert second.already_linked is True
    assert second.id == first.id

    with factory() as session:
        from specsmither.db.models import SpecificationPr

        rows = list(
            session.execute(
                select(SpecificationPr).where(SpecificationPr.specification_id == spec.id)
            ).scalars()
        )
        assert len(rows) == 1
