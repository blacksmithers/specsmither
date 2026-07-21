"""Read/query primitives — the M0 read surface.

The READ surface over the SQLite stores: entity reads/lists for specifications,
epics, tickets and projects, context queries (actionable / blocked / critical-path)
and the cached dependency tree. These are READ-ONLY: every function opens a
short-lived ``session_factory()`` read session, reads through the stores, and NEVER
runs the recompute worklist (only the CRUD mutations do — they own their own
``Session.begin()``). The MCP ``get``/``list`` composers that fan out by ``{type}``
and the dispatch facade are a 0.1.0 concern and live elsewhere.

Three shared read machines:

* **Field selection** (:func:`_select_fields` + the ``*_FIELDS`` allow-lists) — a
  list read projects each record to the wire (camelCase) shape and keeps only the
  requested fields (``id`` is always forced in); unknown fields are reported in
  ``invalid_fields``. No ``fields`` requested returns the full record dump (a
  sensible full set).
* **Pagination** (:func:`_paginate` + ``DEFAULT_LIMIT``) — offset-based: load the
  bounded per-parent list and slice ``items[offset:offset+limit]``. The opaque
  ``next_cursor`` encodes the resume offset (an offset cursor); page two continues
  from it. ``limit`` is clamped to ``[1, MAX_LIMIT]``.
* **Summary vs full** (``summary`` flag, default ``True`` — summary is the default)
  — a single entity get returns either a small summary DTO (a few fields) or the
  full record.

Context + tree are served STRICTLY from the materialized ``Specification.
dependency_tree`` JSON column (built by the recompute worklist). There is no
in-memory fallback: the cached tree is the single source of truth, so the
actionable / blocked / critical-path / dependency-tree reads all read the same
column. A spec that has never been recomputed (no tree) raises
:class:`PreconditionFailedError`.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from specsmither.db.repositories import make_stores
from specsmither.operations.errors import (
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.db.repositories import AllStores, ProjectRecord
    from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord

__all__ = [
    "DEFAULT_LIMIT",
    "EPIC_FIELDS",
    "MAX_LIMIT",
    "PROJECT_FIELDS",
    "SPECIFICATION_FIELDS",
    "TICKET_FIELDS",
    "ActionableResult",
    "ActionableTicket",
    "BlockedResult",
    "BlockedTicket",
    "CriticalPathResult",
    "DependencyRef",
    "EpicSummary",
    "ListPage",
    "SpecificationSummary",
    "TicketRef",
    "TicketSummary",
    "get_blocked_tickets",
    "get_critical_path",
    "get_dependency_tree",
    "get_epic",
    "get_next_actionable_tickets",
    "get_project",
    "get_specification",
    "get_ticket",
    "list_epics",
    "list_projects",
    "list_specifications",
    "list_tickets",
]

_T = TypeVar("_T")

#: Default / max page size.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Field-selection allow-lists (the selectable wire/camelCase keys). A list read may
# project to any subset; ``id`` is always forced in. ``PROJECT_FIELDS`` is the
# wire-key set of ``ProjectRecord`` — projects have no per-field selection of their
# own, so this lets the project list share the uniform field-selection surface.
SPECIFICATION_FIELDS: tuple[str, ...] = (
    "id", "projectId", "specificationTypeId", "schemaVersion", "title", "description",
    "background", "status", "progress", "goals", "requirements",
    "nonFunctionalRequirements", "acceptanceCriteria", "guardrails", "techStack",
    "architecture", "folderStructures", "scope", "sharedPatterns", "epicTargets",
    "fieldDeclarations", "epicCount", "completedEpicCount", "todoEpicCount",
    "inProgressEpicCount", "ticketCount", "completedTicketCount", "pendingTicketCount",
    "readyTicketCount", "activeTicketCount", "estimatedMinutes", "tags", "blueprints",
    "createdAt", "updatedAt", "dependencyTree", "dependencyTreeUpdatedAt",
    "dependencyTreeVersion",
)
EPIC_FIELDS: tuple[str, ...] = (
    "id", "specificationId", "epicNumber", "title", "description", "objective", "order",
    "status", "planningType", "progress", "category", "architecture", "scope", "goals",
    "acceptanceCriteria", "validationCommands", "apiContracts", "sharedPatterns",
    "fileStructures", "requirementsCovered", "nfrsCovered", "goalsCovered",
    "fieldDeclarations", "ticketCount", "completedTicketCount", "pendingTicketCount",
    "readyTicketCount", "activeTicketCount", "estimatedMinutes", "tags", "createdAt",
    "updatedAt",
)
TICKET_FIELDS: tuple[str, ...] = (
    "id", "epicId", "ticketNumber", "title", "description", "status", "ticketType",
    "complexity", "estimatedMinutes", "planningType", "acceptanceCriteria",
    "codeReferences", "typeReferences", "qualityGates", "testCommands", "coverageTarget",
    "notes", "fieldDeclarations", "tags", "blockReason", "progress", "order",
    "createdAt", "updatedAt",
)
PROJECT_FIELDS: tuple[str, ...] = (
    "id", "userId", "name", "description", "status", "specCount", "completedSpecCount",
    "draftSpecCount", "planningSpecCount", "readySpecCount", "inProgressSpecCount",
    "inReviewSpecCount", "epicCount", "completedEpicCount", "ticketCount",
    "completedTicketCount", "createdAt", "updatedAt",
)

#: Canonical ticket statuses (``api-types`` ``TICKET_STATUSES``) for the list filter.
_VALID_TICKET_STATUSES = frozenset({"pending", "ready", "active", "done"})


# --------------------------------------------------------------------------- #
# DTOs / projections                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ListPage:
    """One page of a list read.

    ``items`` are wire (camelCase) dicts — the full record dump when no fields were
    requested, or the projected subset otherwise. ``next_cursor`` is the opaque
    offset cursor to fetch the following page (``None`` at the end);
    ``invalid_fields`` carries any requested field not in the entity's allow-list.
    """

    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_cursor: str | None = None
    invalid_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecificationSummary:
    """The ~few-field spec summary projection (``SpecificationSummary``)."""

    id: str
    title: str
    status: str
    epic_count: int
    ticket_count: int
    completed_ticket_count: int


@dataclass(frozen=True)
class EpicSummary:
    """The ~few-field epic summary projection (``EpicSummary``)."""

    id: str
    epic_number: int | None
    title: str
    status: str
    objective: str
    ticket_count: int
    completed_ticket_count: int


@dataclass(frozen=True)
class TicketSummary:
    """The ~few-field ticket summary projection (``TICKET_SUMMARY_FIELDS``)."""

    id: str
    ticket_number: int | None
    title: str
    status: str
    complexity: str | None
    estimated_minutes: int | None


@dataclass(frozen=True)
class TicketRef:
    """A ticket identity projection used by the context reads."""

    id: str
    ticket_number: int
    title: str
    status: str
    epic_id: str


@dataclass(frozen=True)
class ActionableTicket:
    """A READY ticket from the cached tree (``getNextActionableTickets`` item)."""

    id: str
    ticket_number: int
    title: str
    status: str
    epic_id: str
    specification_id: str


@dataclass(frozen=True)
class DependencyRef:
    """An unsatisfied blocking dependency (``getBlockedTickets`` ``blockedBy`` item)."""

    id: str
    ticket_number: int
    title: str
    status: str


@dataclass(frozen=True)
class BlockedTicket:
    """A blocked ticket + the dependencies still blocking it (from the cached tree)."""

    ticket: TicketRef
    blocked_by: tuple[DependencyRef, ...]


@dataclass(frozen=True)
class ActionableResult:
    """``getNextActionableTickets`` return: the ready slice + total ready count."""

    items: tuple[ActionableTicket, ...]
    total: int


@dataclass(frozen=True)
class BlockedResult:
    """``getBlockedTickets`` return: the blocked tickets + their count."""

    items: tuple[BlockedTicket, ...]
    total: int


@dataclass(frozen=True)
class CriticalPathResult:
    """``_getCriticalPath`` return: the longest incomplete chain + its minutes."""

    path: tuple[TicketRef, ...]
    total_estimated_minutes: int


# --------------------------------------------------------------------------- #
# pagination + field-selection helpers                                        #
# --------------------------------------------------------------------------- #


def _encode_cursor(offset: int) -> str:
    """Encode a resume ``offset`` into an opaque (base64) cursor token."""
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    """Decode an offset cursor; a malformed token is a ``ValidationFailedError``."""
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise ValidationFailedError(f"Invalid pagination cursor: {cursor}") from exc
    if offset < 0:
        raise ValidationFailedError(f"Invalid pagination cursor: {cursor}")
    return offset


def _clamp_limit(limit: int | None) -> int:
    """Clamp ``limit`` into ``[1, MAX_LIMIT]``."""
    value = DEFAULT_LIMIT if limit is None else limit
    return max(1, min(value, MAX_LIMIT))


def _resolve_offset(*, offset: int, cursor: str | None) -> int:
    """A cursor (an encoded offset) takes precedence over an explicit ``offset``."""
    if cursor is not None:
        return _decode_cursor(cursor)
    return max(0, offset)


def _paginate(
    rows: Sequence[_T], *, limit: int, offset: int
) -> tuple[list[_T], int, bool, str | None]:
    """Offset-slice ``rows``; derive ``has_more`` + the cursor."""
    total = len(rows)
    page = list(rows[offset : offset + limit])
    has_more = offset + limit < total
    next_cursor = _encode_cursor(offset + limit) if has_more else None
    return page, total, has_more, next_cursor


def _to_wire(record: BaseModel) -> dict[str, Any]:
    """Dump a record to its JSON-primitive camelCase wire dict (drops ``None``)."""
    return record.model_dump(mode="json", by_alias=True, exclude_none=True)


def _select_fields(
    records: Sequence[BaseModel],
    fields: Sequence[str] | None,
    valid_fields: tuple[str, ...],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Project records to wire dicts, keeping only requested fields.

    No ``fields`` → the full wire dump. Otherwise: ``id`` is forced in, unknown
    fields are reported (``invalid_fields``), and only the fields actually present
    on a record survive.
    """
    wire = [_to_wire(record) for record in records]
    if not fields:
        return wire, ()
    valid_set = frozenset(valid_fields)
    invalid = tuple(f for f in fields if f not in valid_set)
    requested = [f for f in fields if f in valid_set]
    if "id" not in requested:
        requested = ["id", *requested]
    projected = [{key: row[key] for key in requested if key in row} for row in wire]
    return projected, invalid


def _build_page(
    records: Sequence[BaseModel],
    *,
    fields: Sequence[str] | None,
    valid_fields: tuple[str, ...],
    limit: int | None,
    offset: int,
    cursor: str | None,
) -> ListPage:
    """Paginate then field-select a record list into a :class:`ListPage`."""
    page_limit = _clamp_limit(limit)
    page_offset = _resolve_offset(offset=offset, cursor=cursor)
    page, total, has_more, next_cursor = _paginate(records, limit=page_limit, offset=page_offset)
    items, invalid = _select_fields(page, fields, valid_fields)
    return ListPage(
        items=items,
        total=total,
        limit=page_limit,
        offset=page_offset,
        has_more=has_more,
        next_cursor=next_cursor,
        invalid_fields=invalid,
    )


def _require_id(value: str, name: str) -> None:
    """Reject a missing/empty required id argument."""
    if not value:
        raise ValidationFailedError(f"{name} is required and must be a non-empty string")


# --------------------------------------------------------------------------- #
# entity reads / lists                                                        #
# --------------------------------------------------------------------------- #


def get_specification(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    summary: bool = True,
) -> SpecificationRecord | SpecificationSummary:
    """Read one specification — the summary projection (default) or the full record.

    ``summary`` defaults to ``True`` (summary is the default; pass
    ``summary=False`` for the full record). Raises
    :class:`~specsmither.operations.errors.NotFoundError` when the id resolves nothing.
    """
    _require_id(specification_id, "specificationId")
    with session_factory() as session:
        record = make_stores(session).specifications.get_specification(specification_id)
    if summary:
        return SpecificationSummary(
            id=record.id,
            title=record.title,
            status=record.status.value,
            epic_count=record.epic_count,
            ticket_count=record.ticket_count,
            completed_ticket_count=record.completed_ticket_count,
        )
    return record


def get_epic(
    session_factory: sessionmaker[Session],
    epic_id: str,
    *,
    summary: bool = True,
) -> EpicRecord | EpicSummary:
    """Read one epic — the summary projection (default) or the full record."""
    _require_id(epic_id, "epicId")
    with session_factory() as session:
        record = make_stores(session).epics.get_epic(epic_id)
    if summary:
        return EpicSummary(
            id=record.id,
            epic_number=record.epic_number,
            title=record.title,
            status=record.status.value,
            objective=record.objective,
            ticket_count=record.ticket_count,
            completed_ticket_count=record.completed_ticket_count,
        )
    return record


def get_ticket(
    session_factory: sessionmaker[Session],
    ticket_id: str,
    *,
    summary: bool = True,
) -> TicketRecord | TicketSummary:
    """Read one ticket — the summary projection (default) or the full record.

    The ticket store returns ``None`` for a missing id (it does not raise), so this
    raises :class:`~specsmither.operations.errors.NotFoundError` explicitly.
    """
    _require_id(ticket_id, "ticketId")
    with session_factory() as session:
        record = make_stores(session).tickets.get_ticket(ticket_id)
    if record is None:
        raise NotFoundError(f"Ticket not found: {ticket_id}")
    if summary:
        return TicketSummary(
            id=record.id,
            ticket_number=record.ticket_number,
            title=record.title,
            status=record.status.value,
            complexity=record.complexity.value if record.complexity is not None else None,
            estimated_minutes=record.estimated_minutes,
        )
    return record


def get_project(
    session_factory: sessionmaker[Session],
    project_id: str,
) -> ProjectRecord:
    """Read one project (the full record; projects have no summary projection)."""
    _require_id(project_id, "projectId")
    with session_factory() as session:
        return make_stores(session).projects.get_project(project_id)


def list_specifications(
    session_factory: sessionmaker[Session],
    project_id: str,
    *,
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    cursor: str | None = None,
) -> ListPage:
    """List a project's specifications (field-selected + paginated)."""
    _require_id(project_id, "projectId")
    with session_factory() as session:
        records = make_stores(session).specifications.list_specifications(project_id=project_id)
    return _build_page(
        records,
        fields=fields,
        valid_fields=SPECIFICATION_FIELDS,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


def list_epics(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    cursor: str | None = None,
) -> ListPage:
    """List a specification's epics (field-selected + paginated)."""
    _require_id(specification_id, "specificationId")
    with session_factory() as session:
        records = make_stores(session).epics.list_epics(specification_id=specification_id)
    return _build_page(
        records,
        fields=fields,
        valid_fields=EPIC_FIELDS,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


def list_tickets(
    session_factory: sessionmaker[Session],
    *,
    epic_id: str | None = None,
    specification_id: str | None = None,
    status: Sequence[str] | None = None,
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    cursor: str | None = None,
) -> ListPage:
    """List tickets under an epic OR (gathered across epics) a specification.

    Either ``epic_id`` or ``specification_id`` is required. An optional ``status``
    filter keeps only tickets whose status is in the (valid) set, applied in Python
    after the gather: unknown values are dropped, an all-invalid/empty filter is a
    no-op.
    """
    if not epic_id and not specification_id:
        raise ValidationFailedError("Either epicId or specificationId is required")
    with session_factory() as session:
        stores = make_stores(session)
        if epic_id:
            records = stores.tickets.list_tickets(epic_id=epic_id)
        else:
            records = _tickets_for_spec(stores, specification_id or "")
    records = _filter_status(records, status)
    return _build_page(
        records,
        fields=fields,
        valid_fields=TICKET_FIELDS,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


def list_projects(
    session_factory: sessionmaker[Session],
    *,
    user_id: str = "local",
    fields: Sequence[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    cursor: str | None = None,
) -> ListPage:
    """List the user's projects (field-selected + paginated). Single local user."""
    with session_factory() as session:
        records = make_stores(session).projects.list_projects(user_id=user_id)
    return _build_page(
        records,
        fields=fields,
        valid_fields=PROJECT_FIELDS,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


def _tickets_for_spec(stores: AllStores, specification_id: str) -> list[TicketRecord]:
    """Gather a spec's tickets epic-by-epic (epic order, then ticket order)."""
    _require_id(specification_id, "specificationId")
    tickets: list[TicketRecord] = []
    for epic in stores.epics.list_epics(specification_id=specification_id):
        tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
    return tickets


def _filter_status(
    records: list[TicketRecord], status: Sequence[str] | None
) -> list[TicketRecord]:
    """Keep tickets whose status is in the requested (valid) set; else pass through."""
    if not status:
        return records
    wanted = {s for s in status if s in _VALID_TICKET_STATUSES}
    if not wanted:
        return records
    return [t for t in records if t.status.value in wanted]


# --------------------------------------------------------------------------- #
# context + tree (served from the materialized dependency tree)              #
# --------------------------------------------------------------------------- #


def _load_tree(session_factory: sessionmaker[Session], specification_id: str) -> dict[str, Any]:
    """Read the cached ``Specification.dependency_tree`` (the single source of truth).

    Raises :class:`~specsmither.operations.errors.NotFoundError` for a missing spec
    and :class:`PreconditionFailedError` for a spec that has never been recomputed
    (no materialized tree). There is no in-memory fallback walk.
    """
    _require_id(specification_id, "specificationId")
    with session_factory() as session:
        record = make_stores(session).specifications.get_specification(specification_id)
    tree = record.dependency_tree
    if tree is None:
        raise PreconditionFailedError(
            f"Dependency tree not generated for specification {specification_id}. "
            "Apply a mutation (which runs the recompute worklist) to materialize it.",
        )
    return tree


def get_next_actionable_tickets(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    limit: int = 10,
) -> ActionableResult:
    """The READY tickets, served from ``tree.summary.ready_tickets`` (capped to ``limit``).

    ``total`` is the full ready count (not the capped slice).
    """
    tree = _load_tree(session_factory, specification_id)
    tickets = tree["tickets"]
    ready_ids = tree["summary"]["ready_tickets"]
    items = [
        ActionableTicket(
            id=node["id"],
            ticket_number=node["ticket_number"],
            title=node["title"],
            status=node["status"],
            epic_id=node["epic_id"] or "",
            specification_id=specification_id,
        )
        for tid in ready_ids[:limit]
        if (node := tickets.get(tid)) is not None
    ]
    return ActionableResult(items=tuple(items), total=len(ready_ids))


def get_blocked_tickets(
    session_factory: sessionmaker[Session],
    specification_id: str,
) -> BlockedResult:
    """The blocked tickets + their unsatisfied dependencies, from the cached tree."""
    tree = _load_tree(session_factory, specification_id)
    tickets = tree["tickets"]
    items: list[BlockedTicket] = []
    for tid in tree["summary"]["blocked_tickets"]:
        node = tickets.get(tid)
        if node is None:
            continue
        blocked_by = tuple(
            DependencyRef(
                id=dep["id"],
                ticket_number=dep["ticket_number"],
                title=dep["title"],
                status=dep["status"],
            )
            for dep_id in node["unsatisfied_deps"]
            if (dep := tickets.get(dep_id)) is not None
        )
        items.append(
            BlockedTicket(
                ticket=TicketRef(
                    id=node["id"],
                    ticket_number=node["ticket_number"],
                    title=node["title"],
                    status=node["status"],
                    epic_id=node["epic_id"] or "",
                ),
                blocked_by=blocked_by,
            )
        )
    return BlockedResult(items=tuple(items), total=len(items))


def get_critical_path(
    session_factory: sessionmaker[Session],
    specification_id: str,
) -> CriticalPathResult:
    """The critical path (``tree.summary.critical_path`` + its half-up minutes)."""
    tree = _load_tree(session_factory, specification_id)
    tickets = tree["tickets"]
    path: list[TicketRef] = []
    for node in tree["critical_path"]:
        ticket = tickets.get(node["id"])
        path.append(
            TicketRef(
                id=node["id"],
                ticket_number=node["ticket_number"],
                title=node["title"],
                status=ticket["status"] if ticket else "pending",
                epic_id=(ticket["epic_id"] or "") if ticket else "",
            )
        )
    return CriticalPathResult(
        path=tuple(path),
        total_estimated_minutes=tree["summary"]["critical_path_minutes"],
    )


def get_dependency_tree(
    session_factory: sessionmaker[Session],
    specification_id: str,
) -> dict[str, Any]:
    """Return the cached ``Specification.dependency_tree`` JSON.

    Serves the column directly — the tree was materialized by the recompute worklist;
    no work happens here beyond reading it (and the not-generated precondition guard).
    """
    return _load_tree(session_factory, specification_id)
