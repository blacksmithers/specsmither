"""``TicketStoreSqlite`` — the ticket entity repository (work item #8).

The hardest store in M0: a ``Ticket`` is authored/read as one fully-hydrated
:class:`~specsmither.domain.records.TicketRecord` (flat columns + child-backed
arrays), but persisted **decomposed** across the parent ``tickets`` row plus four
owned child tables. This store is the decompose-on-write / recompose-on-read seam.

Owned child tables (REPLACE-ALL on every write — the SQLite analogue of the
AppSync ``relatedReplace``):

* ``acceptance_criteria`` — the BDD ``given`` / ``when`` / ``then`` triple, ordered.
* ``implementation_steps`` — ordered ``text`` rows.
* ``ticket_file_changes`` — one row per planned path, discriminated by ``kind``
  (the ``FileChangeKind`` ``toBeCreated`` / ``toBeModified`` / ``toBeDeleted`` /
  ``toBeReferenced`` literals); the four ``files_to_be_*`` arrays fan out here and
  regroup by ``kind`` on read.
* ``ticket_tests`` — one row per planned ``test_type`` (the only normalized slice
  of the test spec; ``quality_gates`` / ``test_commands`` / ``coverage_target``
  stay flat on the ticket row).

``code_references`` / ``type_references`` / ``field_declarations`` are flat JSON
columns on the ticket row (crucible sub-models, dumped ``by_alias``), NOT child
tables — they round-trip through the row, mirroring :mod:`specsmither.domain.records`.

Faithful-port boundaries (the ``TicketRecord`` DTO is the contract, and it is
deliberately scoped to *this* entity):

* **Counts are never written here.** ``incoming_dep_count`` / ``outgoing_dep_count``
  / ``blueprint_count`` are read back onto the record but only the recompute
  worklist (#14) writes them; the ``update_ticket_*_count`` delta-mutators are
  no-ops (architecture §4, invariant 4).
* **Dependencies, blueprint refs and code/type *snippets* are sibling-owned.**
  ``TicketRecord`` carries no field for them (records.py maps ``code_references`` /
  ``type_references`` to JSON columns, and ``build_spec_full`` assembles the
  ``ticket_dependencies`` / ``ticket_blueprint_refs`` rows from the separate
  ``TicketDependencyStore`` / ``BlueprintStore`` sources). This store therefore
  does not decompose/recompose them — see the module-level open question.

Session-bound: every mutating method runs *inside* the caller's ``Session.begin()``
and never commits; reads use ``self.session``. UNIQUE/constraint violations on a
write are translated through :func:`~specsmither.operations.errors.to_crud_error`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from specsmither.db.models import AcceptanceCriterion as ACRow
from specsmither.db.models import ImplementationStep as StepRow
from specsmither.db.models import Ticket, TicketFileChange, TicketTest
from specsmither.db.repositories.base import SessionStore, require_found
from specsmither.domain.enums import FileChangeKind
from specsmither.domain.records import TicketRecord
from specsmither.operations.errors import to_crud_error

__all__ = ["TicketStoreSqlite"]

_KIND = "ticket"


# ---------------------------------------------------------------------------
# Pure row <-> record mappers (decompose / recompose)
# ---------------------------------------------------------------------------


def _dump_each(items: Sequence[BaseModel]) -> list[dict[str, Any]]:
    """Dump crucible sub-models to the wire (camelCase) JSON shape for storage."""
    return [item.model_dump(by_alias=True, exclude_none=True) for item in items]


def _dump_map(mapping: Mapping[str, BaseModel] | None) -> dict[str, Any] | None:
    """Dump a ``{key: sub-model}`` map (e.g. ``field_declarations``) for storage."""
    if not mapping:
        return None
    return {key: value.model_dump(by_alias=True, exclude_none=True) for key, value in mapping.items()}


def _apply_flat(ticket: Ticket, record: TicketRecord) -> None:
    """Write the record's flat columns onto an ORM ``Ticket`` row.

    Never touches the count columns (recompute-worklist owned) nor the audit
    timestamps (``created_at`` default / ``updated_at`` ``onupdate`` handle them).
    """
    ticket.epic_id = record.epic_id
    ticket.ticket_number = record.ticket_number
    ticket.title = record.title
    ticket.description = record.description
    ticket.status = record.status.value
    ticket.ticket_type = record.ticket_type.value if record.ticket_type is not None else None
    ticket.complexity = record.complexity.value if record.complexity is not None else None
    ticket.estimated_minutes = (
        record.estimated_minutes if record.estimated_minutes is not None else 0
    )
    ticket.planning_type = record.planning_type
    ticket.block_reason = record.block_reason
    # progress is recompute-owned (derived: 100 when done, else 0) — never
    # written here, matching the Epic/Spec stores and the recompute invariant.
    ticket.order = record.order
    ticket.current_work_session_id = record.current_work_session_id
    ticket.tags = record.tags
    ticket.quality_gates = list(record.quality_gates)
    ticket.test_commands = list(record.test_commands)
    ticket.coverage_target = record.coverage_target
    ticket.code_references = _dump_each(record.code_references)
    ticket.type_references = _dump_each(record.type_references)
    ticket.field_declarations = _dump_map(record.field_declarations)


def _ac_rows(record: TicketRecord) -> list[ACRow]:
    """Decompose ``acceptance_criteria`` into ordered child rows (order = 1-based position).

    Positions are 1-based because the recomposed rows feed crucible's
    :class:`~crucible.models.AcceptanceCriterion`, whose ``order`` is constrained
    ``>= 1``; a 0-based position would make every first criterion structurally
    invalid (failing the validator at every phase).
    """
    return [
        ACRow(
            id=ac.id,
            ticket_id=record.id,
            given=ac.given,
            when=ac.when,
            then=ac.then,
            order=index + 1,
        )
        for index, ac in enumerate(record.acceptance_criteria)
    ]


def _step_rows(record: TicketRecord) -> list[StepRow]:
    """Decompose ``implementation_steps`` into ordered child rows (order = 1-based position).

    1-based for the same reason as :func:`_ac_rows`: crucible's
    :class:`~crucible.models.ImplementationStep` constrains ``order`` to ``>= 1``.
    """
    return [
        StepRow(id=step.id, ticket_id=record.id, text=step.text, order=index + 1)
        for index, step in enumerate(record.implementation_steps)
    ]


def _file_change_rows(record: TicketRecord) -> list[TicketFileChange]:
    """Decompose the four ``files_to_be_*`` arrays into ``kind``-tagged child rows."""
    rows: list[TicketFileChange] = []
    order = 0
    groups = (
        (FileChangeKind.TO_BE_CREATED, record.files_to_be_created),
        (FileChangeKind.TO_BE_MODIFIED, record.files_to_be_modified),
        (FileChangeKind.TO_BE_DELETED, record.files_to_be_deleted),
        (FileChangeKind.TO_BE_REFERENCED, record.files_to_be_referenced),
    )
    for kind, paths in groups:
        for path in paths:
            rows.append(
                TicketFileChange(ticket_id=record.id, path=path, kind=kind.value, order=order)
            )
            order += 1
    return rows


def _test_rows(record: TicketRecord) -> list[TicketTest]:
    """Decompose ``test_types`` into ordered planned-test child rows (order = position)."""
    return [
        TicketTest(ticket_id=record.id, test_type=test_type.value, order=index)
        for index, test_type in enumerate(record.test_types)
    ]


def _child_rows(record: TicketRecord) -> list[Any]:
    """The full set of owned child rows for a record (one bulk ``add_all`` payload)."""
    rows: list[Any] = []
    rows.extend(_ac_rows(record))
    rows.extend(_step_rows(record))
    rows.extend(_file_change_rows(record))
    rows.extend(_test_rows(record))
    return rows


def _to_record(
    ticket: Ticket,
    *,
    acceptance_criteria: list[dict[str, Any]],
    implementation_steps: list[dict[str, Any]],
    files: Mapping[str, list[str]],
    test_types: list[str],
) -> TicketRecord:
    """Recompose an ORM row + its loaded child rows into a hydrated ``TicketRecord``.

    Uses ``model_validate`` so pydantic coerces the stored strings/dicts back into
    the typed enum / crucible sub-model shapes (status, file-kind groups, AC triple,
    code/type references). Count columns are read back (not authoritative here).
    """
    data: dict[str, Any] = {
        "id": ticket.id,
        "epic_id": ticket.epic_id,
        "title": ticket.title,
        "ticket_number": ticket.ticket_number,
        "description": ticket.description,
        "status": ticket.status,
        "ticket_type": ticket.ticket_type,
        "complexity": ticket.complexity,
        "estimated_minutes": ticket.estimated_minutes,
        "planning_type": ticket.planning_type,
        "block_reason": ticket.block_reason,
        "progress": ticket.progress,
        "order": ticket.order,
        "current_work_session_id": ticket.current_work_session_id,
        "tags": ticket.tags,
        "incoming_dep_count": ticket.incoming_dep_count,
        "outgoing_dep_count": ticket.outgoing_dep_count,
        "blueprint_count": ticket.blueprint_count,
        "test_types": test_types,
        "quality_gates": ticket.quality_gates or [],
        "test_commands": ticket.test_commands or [],
        "coverage_target": ticket.coverage_target,
        "code_references": ticket.code_references or [],
        "type_references": ticket.type_references or [],
        "field_declarations": ticket.field_declarations,
        "acceptance_criteria": acceptance_criteria,
        "implementation_steps": implementation_steps,
        "files_to_be_created": list(files.get(FileChangeKind.TO_BE_CREATED.value, [])),
        "files_to_be_modified": list(files.get(FileChangeKind.TO_BE_MODIFIED.value, [])),
        "files_to_be_deleted": list(files.get(FileChangeKind.TO_BE_DELETED.value, [])),
        "files_to_be_referenced": list(files.get(FileChangeKind.TO_BE_REFERENCED.value, [])),
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }
    return TicketRecord.model_validate(data)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TicketStoreSqlite(SessionStore):
    """SQLite ``ITicketStore`` impl — decompose-on-write / recompose-on-read."""

    # --- reads ---

    def get_ticket(self, ticket_id: str) -> TicketRecord | None:
        """Load a ticket + all its child rows, recomposed into a ``TicketRecord``."""
        ticket = self.session.get(Ticket, ticket_id)
        if ticket is None:
            return None
        return self._build_records([ticket])[0]

    def list_tickets(
        self,
        *,
        epic_id: str | None = None,
        status: str | None = None,
    ) -> list[TicketRecord]:
        """List tickets (optionally filtered by epic / status), each fully hydrated.

        Collapses the AppSync ``listTicketsFor{Dashboard,Readiness,GateCheck,...}``
        projection variants into one ordered SELECT (projection economy is moot in
        SQLite); children are batch-loaded once for the whole page.
        """
        stmt = select(Ticket)
        if epic_id is not None:
            stmt = stmt.where(Ticket.epic_id == epic_id)
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        stmt = stmt.order_by(Ticket.order, Ticket.id)
        tickets = list(self.session.scalars(stmt).all())
        return self._build_records(tickets)

    # --- writes ---

    def create_ticket(self, record: TicketRecord) -> TicketRecord:
        """Insert the ticket row, then bulk-insert all child rows from its arrays."""
        ticket = Ticket(id=record.id)
        _apply_flat(ticket, record)
        self.session.add(ticket)
        self.session.add_all(_child_rows(record))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise to_crud_error(exc, intent="create") from exc
        return self._build_records([ticket])[0]

    def update_ticket(self, record: TicketRecord) -> TicketRecord:
        """Update the flat ticket row, then REPLACE-ALL every owned child table."""
        ticket = require_found(
            self.session.get(Ticket, record.id), kind=_KIND, entity_id=record.id
        )
        _apply_flat(ticket, record)
        self._delete_children(record.id)
        self.session.add_all(_child_rows(record))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise to_crud_error(exc, intent="update") from exc
        return self._build_records([ticket])[0]

    def delete_ticket(self, ticket_id: str) -> None:
        """Delete the ticket; ``ON DELETE CASCADE`` drops every child row. Idempotent."""
        ticket = self.session.get(Ticket, ticket_id)
        if ticket is not None:
            self.session.delete(ticket)

    # --- count delta-mutators (no-ops) ---

    def update_ticket_incoming_dep_count(self, ticket_id: str, delta: int) -> None:
        """No-op: counts written only by the recompute worklist (#14)."""

    def update_ticket_outgoing_dep_count(self, ticket_id: str, delta: int) -> None:
        """No-op: counts written only by the recompute worklist (#14)."""

    def update_ticket_blueprint_count(self, ticket_id: str, delta: int) -> None:
        """No-op: counts written only by the recompute worklist (#14)."""

    # --- internals ---

    def _delete_children(self, ticket_id: str) -> None:
        """Drop every owned child row for a ticket (the REPLACE-ALL delete phase)."""
        self.session.execute(delete(ACRow).where(ACRow.ticket_id == ticket_id))
        self.session.execute(delete(StepRow).where(StepRow.ticket_id == ticket_id))
        self.session.execute(
            delete(TicketFileChange).where(TicketFileChange.ticket_id == ticket_id)
        )
        self.session.execute(delete(TicketTest).where(TicketTest.ticket_id == ticket_id))

    def _build_records(self, tickets: list[Ticket]) -> list[TicketRecord]:
        """Batch-load the four child tables for ``tickets`` and recompose each record."""
        if not tickets:
            return []
        ids = [ticket.id for ticket in tickets]

        acs_by: dict[str, list[dict[str, Any]]] = {}
        for ac in self.session.scalars(
            select(ACRow).where(ACRow.ticket_id.in_(ids)).order_by(ACRow.order, ACRow.id)
        ).all():
            acs_by.setdefault(ac.ticket_id, []).append(
                {"id": ac.id, "given": ac.given, "when": ac.when, "then": ac.then, "order": ac.order}
            )

        steps_by: dict[str, list[dict[str, Any]]] = {}
        for step in self.session.scalars(
            select(StepRow).where(StepRow.ticket_id.in_(ids)).order_by(StepRow.order, StepRow.id)
        ).all():
            steps_by.setdefault(step.ticket_id, []).append(
                {"id": step.id, "text": step.text, "order": step.order}
            )

        files_by: dict[str, dict[str, list[str]]] = {}
        for fc in self.session.scalars(
            select(TicketFileChange)
            .where(TicketFileChange.ticket_id.in_(ids))
            .order_by(TicketFileChange.order, TicketFileChange.id)
        ).all():
            files_by.setdefault(fc.ticket_id, {}).setdefault(fc.kind, []).append(fc.path)

        tests_by: dict[str, list[str]] = {}
        for test in self.session.scalars(
            select(TicketTest)
            .where(TicketTest.ticket_id.in_(ids))
            .order_by(TicketTest.order, TicketTest.id)
        ).all():
            tests_by.setdefault(test.ticket_id, []).append(test.test_type)

        return [
            _to_record(
                ticket,
                acceptance_criteria=acs_by.get(ticket.id, []),
                implementation_steps=steps_by.get(ticket.id, []),
                files=files_by.get(ticket.id, {}),
                test_types=tests_by.get(ticket.id, []),
            )
            for ticket in tickets
        ]
