"""L4 acceptance: the in-transaction recompute worklist (``rollups.recompute``).

Drives :func:`recompute` against a real on-disk SQLite database (``tmp_path`` via
``init_db`` + ``make_session_factory``), inside genuine mutation transactions, and
asserts the END STATE the worklist materializes: cascaded ticket statuses,
epic/spec/project count columns, per-edge dependency counts, distinct epic
blueprint counts, and the cached dependency tree + its version gate.

Topology: project -> spec -> 1 epic -> tickets A, B, C with ``B requires A`` and
``C requires B`` (all start ``pending``), plus one blueprint linked to A and B in
the same epic. Completing A, then B, then C walks the cascade one hop per pass and
exercises the half-up progress formula (1/3 -> 33, 2/3 -> 67) and the epic-status
derivation (todo -> in_progress -> completed).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    Blueprint,
    Epic,
    Project,
    Specification,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
)
from specsmither.domain.enums import EpicStatus, TicketStatus
from specsmither.rollups.recompute import recompute

# Stable ids so the topology reads clearly in assertions.
PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
EPIC_ID = "01EPIC000000000000000000A"
TICKET_A = "01TICKETA000000000000000A"
TICKET_B = "01TICKETB000000000000000B"
TICKET_C = "01TICKETC000000000000000C"
BLUEPRINT_ID = "01BLUEPRINT00000000000000"


def _open(tmp_path: Path) -> sessionmaker[Session]:
    engine = init_db(tmp_path / "recompute.db")
    return make_session_factory(engine)


def _seed(session: Session) -> None:
    session.add(Project(id=PROJECT_ID, name="P"))
    session.add(
        Specification(id=SPEC_ID, project_id=PROJECT_ID, title="S", specification_type_id=None)
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
        Ticket(id=TICKET_A, epic_id=EPIC_ID, title="A", ticket_number=1, estimated_minutes=10)
    )
    session.add(
        Ticket(id=TICKET_B, epic_id=EPIC_ID, title="B", ticket_number=2, estimated_minutes=20)
    )
    session.add(
        Ticket(id=TICKET_C, epic_id=EPIC_ID, title="C", ticket_number=3, estimated_minutes=30)
    )
    # B requires A ; C requires B.
    session.add(TicketDependency(ticket_id=TICKET_B, depends_on_id=TICKET_A))
    session.add(TicketDependency(ticket_id=TICKET_C, depends_on_id=TICKET_B))
    # One blueprint linked to A and B (same epic) -> epic distinct count == 1.
    session.add(
        Blueprint(
            id=BLUEPRINT_ID, specification_id=SPEC_ID, category="diagram", title="BP", content="x"
        )
    )
    session.add(TicketBlueprintRef(ticket_id=TICKET_A, blueprint_id=BLUEPRINT_ID))
    session.add(TicketBlueprintRef(ticket_id=TICKET_B, blueprint_id=BLUEPRINT_ID))


def _mark_and_recompute(factory: sessionmaker[Session], ticket_id: str, status: str) -> None:
    """In one txn: flip a ticket's authoritative status then recompute the spec."""
    with factory.begin() as session:
        ticket = session.get(Ticket, ticket_id)
        assert ticket is not None
        ticket.status = status
        recompute(session, spec_ids=[SPEC_ID])


def _version(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        return spec.dependency_tree_version


# --------------------------------------------------------------------------- #
# initial recompute                                                            #
# --------------------------------------------------------------------------- #


def test_initial_recompute_cascades_and_counts(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)
        recompute(session, spec_ids=[SPEC_ID], generated_at="2026-06-26T00:00:00+00:00")

    with factory() as session:
        a = session.get(Ticket, TICKET_A)
        b = session.get(Ticket, TICKET_B)
        c = session.get(Ticket, TICKET_C)
        assert a is not None and b is not None and c is not None
        # A has no deps -> ready; B and C still blocked.
        assert a.status == TicketStatus.READY
        assert b.status == TicketStatus.PENDING
        assert c.status == TicketStatus.PENDING
        # A promoted pending -> ready clears its block_reason.
        assert a.block_reason is None

        epic = session.get(Epic, EPIC_ID)
        assert epic is not None
        assert epic.ticket_count == 3
        assert epic.ready_ticket_count == 1
        assert epic.pending_ticket_count == 2
        assert epic.completed_ticket_count == 0
        assert epic.progress == 0
        assert epic.status == EpicStatus.TODO
        assert epic.estimated_minutes == 60  # 10 + 20 + 30
        # 2 intra-epic edges, blueprint linked by A+B counts once.
        assert epic.dependency_count == 2
        assert epic.blueprint_count == 1

        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        assert spec.ticket_count == 3
        assert spec.ready_ticket_count == 1
        assert spec.pending_ticket_count == 2
        assert spec.completed_ticket_count == 0
        assert spec.progress == 0
        assert spec.epic_count == 1
        assert spec.todo_epic_count == 1
        assert spec.dependency_count == 2  # each intra edge counted once
        assert spec.blueprint_count == 1

        # Per-ticket dependency counts: B->A, C->B.
        assert (a.incoming_dep_count, a.outgoing_dep_count) == (1, 0)
        assert (b.incoming_dep_count, b.outgoing_dep_count) == (1, 1)
        assert (c.incoming_dep_count, c.outgoing_dep_count) == (0, 1)
        # Blueprint per-ticket counts.
        assert a.blueprint_count == 1
        assert b.blueprint_count == 1
        assert c.blueprint_count == 0

        # Tree materialized on the first build.
        assert spec.dependency_tree is not None
        assert spec.dependency_tree_version == 1
        assert spec.dependency_tree_updated_at == "2026-06-26T00:00:00+00:00"
        assert spec.critical_path_length > 0

        # Project rollup reflects the single spec.
        project = session.get(Project, PROJECT_ID)
        assert project is not None
        assert project.spec_count == 1
        assert project.ticket_count == 3
        assert project.completed_ticket_count == 0
        assert project.epic_count == 1


# --------------------------------------------------------------------------- #
# progressive completion: A -> B -> C                                          #
# --------------------------------------------------------------------------- #


def test_progressive_completion_cascade_and_progress(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)
        recompute(session, spec_ids=[SPEC_ID])

    versions: list[int] = [_version(factory)]  # after initial build: 1

    # Mark A done -> B promotes pending -> ready; C stays pending (single hop).
    _mark_and_recompute(factory, TICKET_A, TicketStatus.DONE)
    with factory() as session:
        b = session.get(Ticket, TICKET_B)
        c = session.get(Ticket, TICKET_C)
        epic = session.get(Epic, EPIC_ID)
        spec = session.get(Specification, SPEC_ID)
        assert b is not None and c is not None and epic is not None and spec is not None
        assert b.status == TicketStatus.READY
        assert b.block_reason is None
        assert c.status == TicketStatus.PENDING
        assert epic.progress == 33  # 1/3 half-up
        assert spec.progress == 33
        assert epic.status == EpicStatus.IN_PROGRESS
    versions.append(_version(factory))  # structure changed -> 2

    # Mark B done -> C promotes; progress 2/3 -> 67 (half-up, NOT 66).
    _mark_and_recompute(factory, TICKET_B, TicketStatus.DONE)
    with factory() as session:
        c = session.get(Ticket, TICKET_C)
        epic = session.get(Epic, EPIC_ID)
        spec = session.get(Specification, SPEC_ID)
        assert c is not None and epic is not None and spec is not None
        assert c.status == TicketStatus.READY
        assert epic.progress == 67
        assert spec.progress == 67
        assert epic.status == EpicStatus.IN_PROGRESS
    versions.append(_version(factory))  # 3

    # Mark C done -> all done; epic completed, progress 100.
    _mark_and_recompute(factory, TICKET_C, TicketStatus.DONE)
    with factory() as session:
        epic = session.get(Epic, EPIC_ID)
        spec = session.get(Specification, SPEC_ID)
        project = session.get(Project, PROJECT_ID)
        assert epic is not None and spec is not None and project is not None
        assert epic.progress == 100
        assert epic.status == EpicStatus.COMPLETED
        assert spec.progress == 100
        assert spec.completed_ticket_count == 3
        assert project.completed_ticket_count == 3
    versions.append(_version(factory))  # 4

    # Every pass changed ticket status/structure -> version bumped each time.
    assert versions == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# idempotence + fixpoint termination                                          #
# --------------------------------------------------------------------------- #


def test_recompute_is_idempotent_and_terminates(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)
        recompute(session, spec_ids=[SPEC_ID])

    with factory() as session:
        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        first_tree = spec.dependency_tree
        first_version = spec.dependency_tree_version
        first_progress = spec.progress
        first_updated_at = spec.dependency_tree_updated_at

    # Re-running with no authoritative change must NOT bump the version or alter
    # counts (the structural-equality gate skips the write). It also returns —
    # proving the fixpoint loop terminates.
    with factory.begin() as session:
        recompute(session, spec_ids=[SPEC_ID])

    with factory() as session:
        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        assert spec.dependency_tree_version == first_version
        assert spec.dependency_tree == first_tree
        assert spec.dependency_tree_updated_at == first_updated_at
        assert spec.progress == first_progress

    # Duplicate spec ids in one call are de-duped (still idempotent, no raise).
    with factory.begin() as session:
        recompute(session, spec_ids=[SPEC_ID, SPEC_ID])
    with factory() as session:
        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        assert spec.dependency_tree_version == first_version


# --------------------------------------------------------------------------- #
# demote path -> block_reason                                                  #
# --------------------------------------------------------------------------- #


def test_demote_sets_block_reason(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)
        recompute(session, spec_ids=[SPEC_ID])

    # A done -> B ready.
    _mark_and_recompute(factory, TICKET_A, TicketStatus.DONE)

    # Flip A to 'active' (sticky, won't re-promote) -> B demotes ready -> pending
    # and gets a block_reason naming its single unsatisfied dependency (A).
    _mark_and_recompute(factory, TICKET_A, TicketStatus.ACTIVE)
    with factory() as session:
        a = session.get(Ticket, TICKET_A)
        b = session.get(Ticket, TICKET_B)
        assert a is not None and b is not None
        assert a.status == TicketStatus.ACTIVE  # sticky
        assert b.status == TicketStatus.PENDING
        assert b.block_reason is not None
        assert b.block_reason.startswith("Blocked by 1 dependency:")
        assert "A" in b.block_reason  # A's title
