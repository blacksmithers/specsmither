"""L7 acceptance: the read/query primitives (``operations.queries``).

Builds a real on-disk SQLite project -> spec -> epic with 25 tickets (and a small
dependency chain ``T02 requires T01``, ``T03 requires T02``), runs the recompute
worklist ONCE so the materialized dependency tree + counts exist, then drives the
READ-ONLY query surface against a short-lived read session:

* list pagination returns ``DEFAULT_LIMIT`` items + a working ``next_cursor`` that
  continues correctly onto page two (no overlap, full coverage);
* field-selection returns ONLY the requested fields (``id`` always forced in) and
  reports unknown fields;
* summary mode vs full mode return the small DTO vs the full record;
* ``get_next_actionable_tickets`` returns the ready tickets and
  ``get_blocked_tickets`` the blocked ones (with their blockers) from the tree;
* ``get_critical_path`` returns the longest incomplete chain;
* ``get_dependency_tree`` returns the cached tree with ``version >= 1``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import Epic, Project, Specification, Ticket, TicketDependency
from specsmither.domain.records import SpecificationRecord, TicketRecord
from specsmither.operations.errors import NotFoundError, PreconditionFailedError
from specsmither.operations.queries import (
    DEFAULT_LIMIT,
    SpecificationSummary,
    TicketSummary,
    get_blocked_tickets,
    get_critical_path,
    get_dependency_tree,
    get_next_actionable_tickets,
    get_specification,
    get_ticket,
    list_tickets,
)
from specsmither.rollups.recompute import recompute

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
SPEC_ID_EMPTY = "01SPEC000000000000000000B"  # seeded but never recomputed (no tree)
EPIC_ID = "01EPIC000000000000000000A"
TICKET_COUNT = 25
GEN_AT = "2026-06-26T00:00:00+00:00"


def _ticket_id(n: int) -> str:
    """A stable 26-char ticket id that sorts by ``n`` (``01TICKET`` + 18 digits)."""
    return f"01TICKET{n:018d}"


def _estimate(n: int) -> int:
    return {1: 10, 2: 20, 3: 30}.get(n, 5)


def _build(tmp_path: Path) -> sessionmaker[Session]:
    """Seed the topology and run the recompute worklist once; return the factory."""
    engine = init_db(tmp_path / "queries.db")
    factory = make_session_factory(engine)
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="P"))
        session.add(Specification(id=SPEC_ID, project_id=PROJECT_ID, title="S"))
        session.add(Specification(id=SPEC_ID_EMPTY, project_id=PROJECT_ID, title="empty"))
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
        for n in range(1, TICKET_COUNT + 1):
            session.add(
                Ticket(
                    id=_ticket_id(n),
                    epic_id=EPIC_ID,
                    title=f"T{n:02d}",
                    ticket_number=n,
                    order=n,
                    estimated_minutes=_estimate(n),
                    complexity="medium" if n == 1 else None,
                )
            )
        # T02 requires T01 ; T03 requires T02 -> T02/T03 blocked, the rest ready.
        session.add(TicketDependency(ticket_id=_ticket_id(2), depends_on_id=_ticket_id(1)))
        session.add(TicketDependency(ticket_id=_ticket_id(3), depends_on_id=_ticket_id(2)))
        recompute(session, spec_ids=[SPEC_ID], generated_at=GEN_AT)
    return factory


# --------------------------------------------------------------------------- #
# pagination                                                                  #
# --------------------------------------------------------------------------- #


def test_list_pagination_default_limit_and_cursor_continuation(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    page1 = list_tickets(factory, epic_id=EPIC_ID)
    assert page1.limit == DEFAULT_LIMIT
    assert len(page1.items) == DEFAULT_LIMIT  # 20
    assert page1.total == TICKET_COUNT
    assert page1.has_more is True
    assert page1.next_cursor is not None

    page2 = list_tickets(factory, epic_id=EPIC_ID, cursor=page1.next_cursor)
    assert len(page2.items) == TICKET_COUNT - DEFAULT_LIMIT  # 5
    assert page2.has_more is False
    assert page2.next_cursor is None

    # Page two continues correctly: no overlap, full coverage, in order.
    ids1 = [item["id"] for item in page1.items]
    ids2 = [item["id"] for item in page2.items]
    assert set(ids1).isdisjoint(ids2)
    assert ids1 + ids2 == [_ticket_id(n) for n in range(1, TICKET_COUNT + 1)]


# --------------------------------------------------------------------------- #
# field selection                                                             #
# --------------------------------------------------------------------------- #


def test_field_selection_returns_only_requested_fields(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    page = list_tickets(factory, epic_id=EPIC_ID, fields=["title", "status"])
    assert page.invalid_fields == ()
    for item in page.items:
        assert set(item.keys()) == {"id", "title", "status"}  # id is always forced in


def test_field_selection_reports_unknown_fields(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    page = list_tickets(factory, epic_id=EPIC_ID, fields=["title", "bogusField"])
    assert page.invalid_fields == ("bogusField",)
    for item in page.items:
        assert set(item.keys()) == {"id", "title"}


# --------------------------------------------------------------------------- #
# summary vs full                                                             #
# --------------------------------------------------------------------------- #


def test_ticket_summary_vs_full_mode(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    summary = get_ticket(factory, _ticket_id(1), summary=True)
    assert isinstance(summary, TicketSummary)
    assert summary.ticket_number == 1
    assert summary.status == "ready"
    assert summary.complexity == "medium"
    assert summary.estimated_minutes == 10

    full = get_ticket(factory, _ticket_id(1), summary=False)
    assert isinstance(full, TicketRecord)
    assert full.epic_id == EPIC_ID
    assert full.status.value == "ready"
    # The full record exposes fields the summary never carries.
    assert hasattr(full, "acceptance_criteria")


def test_specification_summary_vs_full_mode(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    summary = get_specification(factory, SPEC_ID, summary=True)
    assert isinstance(summary, SpecificationSummary)
    assert summary.ticket_count == TICKET_COUNT
    assert summary.epic_count == 1

    full = get_specification(factory, SPEC_ID, summary=False)
    assert isinstance(full, SpecificationRecord)
    assert full.project_id == PROJECT_ID
    assert full.dependency_tree is not None


# --------------------------------------------------------------------------- #
# context: actionable / blocked / critical path                              #
# --------------------------------------------------------------------------- #


def test_get_next_actionable_tickets_from_tree(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    result = get_next_actionable_tickets(factory, SPEC_ID, limit=10)
    # Ready = T01 + T04..T25 = 23; the limit caps the slice but not the total.
    assert result.total == 23
    assert len(result.items) == 10
    ids = {item.id for item in result.items}
    assert _ticket_id(1) in ids
    assert _ticket_id(2) not in ids  # blocked
    assert all(item.specification_id == SPEC_ID for item in result.items)
    assert all(item.status == "ready" for item in result.items)


def test_get_blocked_tickets_from_tree(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    result = get_blocked_tickets(factory, SPEC_ID)
    blocked_by_id = {bt.ticket.id: bt for bt in result.items}
    assert set(blocked_by_id) == {_ticket_id(2), _ticket_id(3)}
    assert result.total == 2

    # T02 is blocked by exactly T01 (still not done).
    t2 = blocked_by_id[_ticket_id(2)]
    assert [dep.id for dep in t2.blocked_by] == [_ticket_id(1)]


def test_get_critical_path_from_tree(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    result = get_critical_path(factory, SPEC_ID)
    assert {node.id for node in result.path} == {_ticket_id(1), _ticket_id(2), _ticket_id(3)}
    assert len(result.path) == 3
    assert result.total_estimated_minutes == 60  # 10 + 20 + 30


# --------------------------------------------------------------------------- #
# tree                                                                        #
# --------------------------------------------------------------------------- #


def test_get_dependency_tree_returns_cached_tree(tmp_path: Path) -> None:
    factory = _build(tmp_path)

    tree = get_dependency_tree(factory, SPEC_ID)
    assert tree["specification_id"] == SPEC_ID
    assert tree["version"] >= 1
    assert "summary" in tree
    assert "tickets" in tree
    assert len(tree["tickets"]) == TICKET_COUNT


# --------------------------------------------------------------------------- #
# error paths                                                                 #
# --------------------------------------------------------------------------- #


def test_get_dependency_tree_without_recompute_raises_precondition(tmp_path: Path) -> None:
    factory = _build(tmp_path)
    try:
        get_dependency_tree(factory, SPEC_ID_EMPTY)
    except PreconditionFailedError:
        pass
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("expected PreconditionFailedError for an un-recomputed spec")


def test_get_ticket_missing_raises_not_found(tmp_path: Path) -> None:
    factory = _build(tmp_path)
    try:
        get_ticket(factory, "01TICKETMISSING00000000000")
    except NotFoundError:
        pass
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("expected NotFoundError for a missing ticket id")
