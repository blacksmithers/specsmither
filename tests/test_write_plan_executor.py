"""L6 acceptance: the WritePlan executor (#16) routes mutations through recompute (#14).

Drives :func:`commit_write_plan` against a real on-disk SQLite database (``tmp_path``
via ``init_db`` + ``make_session_factory``) and asserts BOTH halves of the seam:

1. the raw ORM writes landed — the ``specMutation`` updated the ticket row, the
   ``relatedPut`` inserted a ticket dependency (with camelCase keys, exercising the
   ``to_snake`` normaliser), and the ``relatedReplace`` truly replaced the ticket's
   acceptance criteria (2 rows in → 1 row out);
2. the in-transaction recompute worklist RAN as part of the executor path — the
   epic/spec denormalized counts reflect the new rows (``completed_ticket_count == 1``),
   progress was recomputed, the dependency edge promoted its dependent via the status
   cascade, the per-edge dependency counts were written, and the cached dependency
   tree was materialised (``dependency_tree_version >= 1``).

Topology: project → spec → 1 epic → tickets A, B (both ``pending``); A is pre-seeded
with two acceptance criteria. The plan marks A done, adds ``B requires A``, and
replaces A's acceptance criteria with a single new row.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.adapters.write_plan_executor import (
    RelatedPut,
    RelatedReplace,
    SpecMutation,
    WritePlan,
    commit_write_plan,
)
from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    AcceptanceCriterion,
    Epic,
    Project,
    Specification,
    Ticket,
    TicketDependency,
)
from specsmither.domain.enums import TicketStatus

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
EPIC_ID = "01EPIC000000000000000000A"
TICKET_A = "01TICKETA000000000000000A"
TICKET_B = "01TICKETB000000000000000B"
GEN_AT = "2026-06-26T00:00:00+00:00"


def _open(tmp_path: Path) -> sessionmaker[Session]:
    engine = init_db(tmp_path / "write_plan.db")
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
    # The ticket child tables carry no ORM relationship back to Ticket (they are
    # decompose-on-write rows), so the unit of work won't order the ticket inserts
    # ahead of their FK children — flush the spine before adding the child rows.
    session.flush()
    # Two acceptance criteria on A — the relatedReplace must collapse these to one.
    session.add(
        AcceptanceCriterion(ticket_id=TICKET_A, given="g0", when="w0", then="t0", order=0)
    )
    session.add(
        AcceptanceCriterion(ticket_id=TICKET_A, given="g0b", when="w0b", then="t0b", order=1)
    )


def test_executor_writes_rows_and_runs_recompute(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)

    plan = WritePlan(
        items=[
            # (a) specMutation: flip ticket A to done.
            SpecMutation(entity_type="ticket", entity_id=TICKET_A, fields={"status": "done"}),
            # (b) relatedPut: B requires A — camelCase keys exercise to_snake().
            RelatedPut(
                table="ticket_dependencies",
                item={"ticketId": TICKET_B, "dependsOnId": TICKET_A},
            ),
            # (c) relatedReplace: collapse A's two acceptance criteria to one.
            RelatedReplace(
                table="acceptance_criteria",
                parent_field="ticket_id",
                parent_id=TICKET_A,
                items=[{"given": "g1", "when": "w1", "then": "t1", "order": 0}],
            ),
        ],
        description="test: status + dependency + AC replace",
    )

    # The executor opens its own one-txn-per-mutation transaction (BEGIN IMMEDIATE),
    # applies the items, runs recompute, and commits.
    commit_write_plan(factory, plan, generated_at=GEN_AT)

    with factory() as session:
        # --- (1) raw ORM writes landed -------------------------------------- #
        ticket_a = session.get(Ticket, TICKET_A)
        ticket_b = session.get(Ticket, TICKET_B)
        assert ticket_a is not None and ticket_b is not None
        assert ticket_a.status == TicketStatus.DONE

        deps = list(
            session.execute(
                select(TicketDependency).where(TicketDependency.ticket_id == TICKET_B)
            ).scalars()
        )
        assert len(deps) == 1
        assert deps[0].depends_on_id == TICKET_A

        criteria = list(
            session.execute(
                select(AcceptanceCriterion).where(AcceptanceCriterion.ticket_id == TICKET_A)
            ).scalars()
        )
        assert len(criteria) == 1  # 2 seeded rows replaced by 1.
        assert (criteria[0].given, criteria[0].when, criteria[0].then) == ("g1", "w1", "t1")

        # --- (2) recompute worklist ran as part of the executor path -------- #
        # Status cascade: A done ⇒ B (requires A) promoted pending → ready.
        assert ticket_b.status == TicketStatus.READY
        # Per-edge dependency counts written by recompute.
        assert ticket_a.incoming_dep_count == 1
        assert ticket_b.outgoing_dep_count == 1

        epic = session.get(Epic, EPIC_ID)
        assert epic is not None
        assert epic.ticket_count == 2
        assert epic.completed_ticket_count == 1
        assert epic.ready_ticket_count == 1
        assert epic.progress == 50  # 1 of 2 done, half-up.
        assert epic.dependency_count == 1

        spec = session.get(Specification, SPEC_ID)
        assert spec is not None
        assert spec.ticket_count == 2
        assert spec.completed_ticket_count == 1
        assert spec.progress == 50
        assert spec.dependency_count == 1
        # Cached dependency tree materialised in the same transaction.
        assert spec.dependency_tree is not None
        assert spec.dependency_tree_version >= 1
        assert spec.dependency_tree_updated_at == GEN_AT

        # Project rollup recomputed from the spec rows.
        project = session.get(Project, PROJECT_ID)
        assert project is not None
        assert project.ticket_count == 2
        assert project.completed_ticket_count == 1
