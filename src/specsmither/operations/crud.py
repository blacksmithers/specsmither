"""M0 CRUD mutation primitives — work item #18.

A clean Python rewrite of the SpecForge operations CRUD surface
(``packages/operations/src/operations/{create,update,delete,bulk,dependencies,
blueprints}.ts`` + the inline freeze guards from ``guards/``), rebound onto the
SQLite ``*StoreSqlite`` repositories.

Every function here is a **mutation primitive**: it owns ITS OWN transaction and
runs the in-transaction recompute worklist before commit (architecture invariant
3 — the M0 acceptance hinges on this)::

    with session_factory.begin() as session:        # BEGIN IMMEDIATE
        stores = make_stores(session)
        ... apply the mutation via the stores ...
        recompute(session, spec_ids=[affected_spec_id])
        ... read the affected record(s) back via the stores ...
    # the begin() context commits on exit; rolls back on any exception

The TS handlers/stores NEVER recomputed (DynamoDB streams did) — so the
:func:`~specsmither.rollups.recompute.recompute` call is ADDED here; without it
the denormalized counts / progress / dependency tree silently stop updating.

What is dropped versus the TS originals (single-user, NO-LLM, no cloud):

* auth / ``requireProjectAccess`` (single local user) — only the entity-existence
  check survives (missing parent → :class:`NotFoundError`);
* the injected ``scoreX`` / ``applyGatedPhaseTransition`` / ``normalizeImplementation``
  / ``freezeSpecSnapshots`` callbacks (scoring / lifecycle / checklist / config);
* ``delete.ts``'s ``appSyncClient.mutate(deleteXAtomic)`` cascade — replaced by a
  single ORM delete that leans on ``FOREIGN KEY ... ON DELETE CASCADE``.

The freeze guard is the one PURE domain rule kept verbatim: a spec in ``done`` /
``reviewed`` is read-only (blocks update), and ``done`` / ``reviewed`` /
``in_review`` block creating child entities under it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from crucible.models.enums import DependencyType
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from specsmither.db.base import new_ulid
from specsmither.db.migrations import get_default_specification_type_id
from specsmither.db.models import (
    Epic,
    Project,
    Specification,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
)
from specsmither.db.repositories import make_stores
from specsmither.domain.enums import SpecStatus, TicketStatus
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
)
from specsmither.operations.errors import (
    ConflictError,
    CrudError,
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
    to_crud_error,
)
from specsmither.rollups.counts import SpecCountsView, derive_project_counts
from specsmither.rollups.recompute import recompute

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "BulkAddResult",
    "DeleteResult",
    "DependencyFailure",
    "DependencyRequest",
    "add_dependency",
    "bulk_add_dependencies",
    "create_blueprint",
    "create_epic",
    "create_specification",
    "create_ticket",
    "delete_blueprint",
    "delete_epic",
    "delete_specification",
    "delete_ticket",
    "link_blueprint_to_ticket",
    "remove_dependency",
    "unlink_blueprint_from_ticket",
    "update_blueprint",
    "update_epic",
    "update_specification",
    "update_ticket",
    "would_create_cycle",
]

#: Spec statuses that block creating child entities under the spec (read-only +
#: the explicit ``in_review`` create-block).
_CREATE_BLOCKED: frozenset[str] = frozenset(
    {SpecStatus.DONE.value, SpecStatus.REVIEWED.value, SpecStatus.IN_REVIEW.value}
)
#: Spec statuses that make an entity read-only (block updates).
_UPDATE_BLOCKED: frozenset[str] = frozenset({SpecStatus.DONE.value, SpecStatus.REVIEWED.value})

#: Hard cap on a single ``bulk_add_dependencies`` batch (the OPERATIONS primitive;
#: distinct from the planning ``create_dependencies`` ``maxBatch=5000``).
MAX_BULK_DEPENDENCIES = 100

_R = TypeVar("_R", bound=BaseModel)


# --------------------------------------------------------------------------- #
# result shapes                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeleteResult:
    """Outcome of a delete: the removed id + the surviving parent ids it recomputed."""

    deleted_id: str
    specification_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True)
class DependencyRequest:
    """One ``ticket → depends_on`` edge to add in a :func:`bulk_add_dependencies` batch."""

    ticket_id: str
    depends_on_id: str
    type: DependencyType | str = DependencyType.REQUIRES


@dataclass(frozen=True)
class DependencyFailure:
    """A rejected item in a bulk dependency batch (per-item partial success)."""

    ticket_id: str
    depends_on_id: str
    error: str


@dataclass(frozen=True)
class BulkAddResult:
    """Per-item partial-success outcome of :func:`bulk_add_dependencies`."""

    added: int
    failed: list[DependencyFailure] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# internal helpers                                                             #
# --------------------------------------------------------------------------- #


def _require_title(title: str) -> str:
    """Return the trimmed title, or raise :class:`ValidationFailedError` if empty."""
    if not title or not title.strip():
        raise ValidationFailedError(
            "title is required and must be a non-empty string.", context={"field": "title"}
        )
    return title.strip()


def _apply_changes(record: _R, changes: Mapping[str, Any]) -> _R:
    """Merge ``changes`` (snake_case record fields) onto a copy of ``record``.

    Dumps the record to a plain dict, overlays ``changes``, and re-validates so a
    string value (e.g. ``status="done"``) is coerced back to its typed enum /
    sub-model. A rejected value (e.g. a planning-phase string passed as
    ``Spec.status``, the M4.13 rule) surfaces as :class:`ValidationFailedError`.
    """
    data = record.model_dump(by_alias=False)
    data.update(changes)
    try:
        return type(record).model_validate(data)
    except ValidationError as exc:
        raise ValidationFailedError(f"Invalid update: {exc}", cause=exc) from exc


def _spec_status(session: Session, spec_id: str) -> str:
    """Return a spec's lifecycle status, or raise :class:`NotFoundError` if absent."""
    spec = session.get(Specification, spec_id)
    if spec is None:
        raise NotFoundError(f"Specification not found: {spec_id}")
    return spec.status


def _epic_spec_id(session: Session, epic_id: str) -> str:
    """Resolve an epic to its owning spec id, or raise :class:`NotFoundError`."""
    epic = session.get(Epic, epic_id)
    if epic is None:
        raise NotFoundError(f"Epic not found: {epic_id}")
    return epic.specification_id


def _ticket_spec_id(session: Session, ticket_id: str) -> str | None:
    """Resolve a ticket to its owning spec id (ticket → epic → spec); ``None`` if gone."""
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return None
    epic = session.get(Epic, ticket.epic_id)
    return epic.specification_id if epic is not None else None


def _assert_creatable(status: str, spec_id: str) -> None:
    """Freeze guard: block creating a child entity under a frozen / in-review spec."""
    if status in _CREATE_BLOCKED:
        raise PreconditionFailedError(
            f"Cannot create child entity: specification {spec_id} is in status "
            f"'{status}' and is not editable.",
            context={"specification_id": spec_id, "status": status},
        )


def _assert_editable(status: str, spec_id: str) -> None:
    """Freeze guard: block updating a ``done`` / ``reviewed`` (read-only) entity."""
    if status in _UPDATE_BLOCKED:
        raise PreconditionFailedError(
            f"Cannot update: specification {spec_id} is read-only (status '{status}').",
            context={"specification_id": spec_id, "status": status},
        )


def _next_ticket_number(session: Session, epic_id: str) -> int:
    """``max(ticket_number) + 1`` within an epic (``1`` for the first ticket)."""
    current = session.execute(
        select(func.max(Ticket.ticket_number)).where(Ticket.epic_id == epic_id)
    ).scalar()
    return (current or 0) + 1


def _next_epic_number(session: Session, spec_id: str) -> int:
    """``max(epic_number) + 1`` within a spec (``1`` for the first epic)."""
    current = session.execute(
        select(func.max(Epic.epic_number)).where(Epic.specification_id == spec_id)
    ).scalar()
    return (current or 0) + 1


def _recompute_spec_ids(spec_ids: Sequence[str | None]) -> list[str]:
    """Drop ``None``s (a gone parent) from a recompute worklist."""
    return [sid for sid in spec_ids if sid is not None]


# --------------------------------------------------------------------------- #
# create                                                                       #
# --------------------------------------------------------------------------- #


def create_specification(
    session_factory: sessionmaker[Session],
    *,
    project_id: str,
    title: str,
    fields: Mapping[str, Any] | None = None,
) -> SpecificationRecord:
    """Create a ``draft`` specification under ``project_id`` and recompute the project.

    Resolves the seeded default ``specification_type`` when none is supplied.
    ``fields`` carries any additional snake_case :class:`SpecificationRecord`
    columns (content arrays, ``background``, ``tags`` …).
    """
    clean_title = _require_title(title)
    with session_factory.begin() as session:
        stores = make_stores(session)
        data: dict[str, Any] = dict(fields or {})
        data["id"] = new_ulid()
        data["project_id"] = project_id
        data["title"] = clean_title
        data.setdefault("status", SpecStatus.DRAFT.value)
        if not data.get("specification_type_id"):
            data["specification_type_id"] = get_default_specification_type_id(session)
        record = SpecificationRecord.model_validate(data)
        created = stores.specifications.create_specification(record)
        recompute(session, spec_ids=[created.id])
        return stores.specifications.get_specification(created.id)


def create_epic(
    session_factory: sessionmaker[Session],
    *,
    specification_id: str,
    title: str,
    description: str = "",
    objective: str = "",
    fields: Mapping[str, Any] | None = None,
) -> EpicRecord:
    """Create an epic under ``specification_id`` (freeze-guarded) and recompute the spec.

    The epic number / order are assigned as ``max + 1`` within the spec unless
    supplied in ``fields``.
    """
    clean_title = _require_title(title)
    with session_factory.begin() as session:
        stores = make_stores(session)
        _assert_creatable(_spec_status(session, specification_id), specification_id)
        data: dict[str, Any] = dict(fields or {})
        data["id"] = new_ulid()
        data["specification_id"] = specification_id
        data["title"] = clean_title
        data.setdefault("description", description)
        data.setdefault("objective", objective)
        data.setdefault("epic_number", _next_epic_number(session, specification_id))
        data.setdefault("order", data["epic_number"])
        record = EpicRecord.model_validate(data)
        created = stores.epics.create_epic(record)
        recompute(session, spec_ids=[specification_id])
        return stores.epics.get_epic(created.id)


def create_ticket(
    session_factory: sessionmaker[Session],
    *,
    epic_id: str,
    title: str,
    fields: Mapping[str, Any] | None = None,
) -> TicketRecord:
    """Create a ticket under ``epic_id`` (freeze-guarded) and recompute the spec.

    Seeded ``ready`` (the TS create default); the recompute cascade then re-derives
    its status from any dependencies. Child arrays (acceptance criteria,
    implementation steps, the four ``files_to_be_*`` lists, ``test_types``) ride in
    via ``fields`` and are decomposed into the owned child tables by the store.
    """
    clean_title = _require_title(title)
    with session_factory.begin() as session:
        stores = make_stores(session)
        spec_id = _epic_spec_id(session, epic_id)
        _assert_creatable(_spec_status(session, spec_id), spec_id)
        data: dict[str, Any] = dict(fields or {})
        data["id"] = new_ulid()
        data["epic_id"] = epic_id
        data["title"] = clean_title
        data.setdefault("status", TicketStatus.READY.value)
        data.setdefault("ticket_number", _next_ticket_number(session, epic_id))
        data.setdefault("order", data["ticket_number"])
        record = TicketRecord.model_validate(data)
        created = stores.tickets.create_ticket(record)
        recompute(session, spec_ids=[spec_id])
        result = stores.tickets.get_ticket(created.id)
        assert result is not None  # just written in this txn
        return result


def create_blueprint(
    session_factory: sessionmaker[Session],
    *,
    specification_id: str,
    title: str,
    category: str,
    fields: Mapping[str, Any] | None = None,
) -> BlueprintRecord:
    """Create a blueprint under ``specification_id`` (freeze-guarded) and recompute the spec."""
    clean_title = _require_title(title)
    with session_factory.begin() as session:
        stores = make_stores(session)
        _assert_creatable(_spec_status(session, specification_id), specification_id)
        data: dict[str, Any] = dict(fields or {})
        data["id"] = new_ulid()
        data["specification_id"] = specification_id
        data["title"] = clean_title
        data["category"] = category
        data.setdefault("content", "")
        record = BlueprintRecord.model_validate(data)
        created = stores.blueprints.create_blueprint(record)
        recompute(session, spec_ids=[specification_id])
        return stores.blueprints.get_blueprint(created.id)


# --------------------------------------------------------------------------- #
# update                                                                       #
# --------------------------------------------------------------------------- #


def update_specification(
    session_factory: sessionmaker[Session],
    specification_id: str,
    changes: Mapping[str, Any],
) -> SpecificationRecord:
    """Update a spec's fields (freeze-guarded read-only check on its own status).

    ``changes`` is a snake_case map of :class:`SpecificationRecord` fields. A
    ``status`` value that is not a valid :class:`SpecStatus` (e.g. a planning phase,
    the M4.13 rule) is rejected with :class:`ValidationFailedError`.
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        existing = stores.specifications.get_specification(specification_id)
        _assert_editable(existing.status.value, specification_id)
        updated = _apply_changes(existing, changes)
        stores.specifications.update_specification(updated)
        recompute(session, spec_ids=[specification_id])
        return stores.specifications.get_specification(specification_id)


def update_epic(
    session_factory: sessionmaker[Session],
    epic_id: str,
    changes: Mapping[str, Any],
) -> EpicRecord:
    """Update an epic's fields (freeze-guarded on the owning spec) and recompute it."""
    with session_factory.begin() as session:
        stores = make_stores(session)
        existing = stores.epics.get_epic(epic_id)
        spec_id = existing.specification_id
        _assert_editable(_spec_status(session, spec_id), spec_id)
        updated = _apply_changes(existing, changes)
        stores.epics.update_epic(updated)
        recompute(session, spec_ids=[spec_id])
        return stores.epics.get_epic(epic_id)


def update_ticket(
    session_factory: sessionmaker[Session],
    ticket_id: str,
    changes: Mapping[str, Any],
) -> TicketRecord:
    """Update a ticket (freeze-guarded) and recompute the spec.

    Child arrays present in ``changes`` (acceptance criteria, steps, file lists,
    test types) are REPLACE-ALL'd by the store's decompose path; arrays absent from
    ``changes`` are preserved (the merged record carries the existing rows).
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        existing = stores.tickets.get_ticket(ticket_id)
        if existing is None:
            raise NotFoundError(f"Ticket not found: {ticket_id}")
        spec_id = _epic_spec_id(session, existing.epic_id)
        _assert_editable(_spec_status(session, spec_id), spec_id)
        updated = _apply_changes(existing, changes)
        stores.tickets.update_ticket(updated)
        recompute(session, spec_ids=[spec_id])
        result = stores.tickets.get_ticket(ticket_id)
        assert result is not None  # just updated in this txn
        return result


def update_blueprint(
    session_factory: sessionmaker[Session],
    blueprint_id: str,
    changes: Mapping[str, Any],
) -> BlueprintRecord:
    """Update a blueprint's fields (freeze-guarded on the owning spec) and recompute."""
    with session_factory.begin() as session:
        stores = make_stores(session)
        existing = stores.blueprints.get_blueprint(blueprint_id)
        spec_id = existing.specification_id
        _assert_editable(_spec_status(session, spec_id), spec_id)
        updated = _apply_changes(existing, changes)
        stores.blueprints.update_blueprint(updated)
        recompute(session, spec_ids=[spec_id])
        return stores.blueprints.get_blueprint(blueprint_id)


# --------------------------------------------------------------------------- #
# delete                                                                       #
# --------------------------------------------------------------------------- #


def delete_ticket(session_factory: sessionmaker[Session], ticket_id: str) -> DeleteResult:
    """Delete a ticket (cascade drops its child rows) and recompute the surviving spec."""
    with session_factory.begin() as session:
        stores = make_stores(session)
        if stores.tickets.get_ticket(ticket_id) is None:
            raise NotFoundError(f"Ticket not found: {ticket_id}")
        spec_id = _ticket_spec_id(session, ticket_id)
        project_id = _spec_project_id(session, spec_id)
        stores.tickets.delete_ticket(ticket_id)
        session.flush()
        recompute(session, spec_ids=_recompute_spec_ids([spec_id]))
        return DeleteResult(deleted_id=ticket_id, specification_id=spec_id, project_id=project_id)


def delete_epic(session_factory: sessionmaker[Session], epic_id: str) -> DeleteResult:
    """Delete an epic (cascade drops its tickets + their children) and recompute the spec."""
    with session_factory.begin() as session:
        stores = make_stores(session)
        spec_id = _epic_spec_id(session, epic_id)  # also the existence check
        project_id = _spec_project_id(session, spec_id)
        stores.epics.delete_epic(epic_id)
        session.flush()
        recompute(session, spec_ids=[spec_id])
        return DeleteResult(deleted_id=epic_id, specification_id=spec_id, project_id=project_id)


def delete_blueprint(session_factory: sessionmaker[Session], blueprint_id: str) -> DeleteResult:
    """Delete a blueprint (cascade drops its ticket refs) and recompute the spec."""
    with session_factory.begin() as session:
        stores = make_stores(session)
        existing = stores.blueprints.get_blueprint(blueprint_id)  # existence check
        spec_id = existing.specification_id
        project_id = _spec_project_id(session, spec_id)
        stores.blueprints.delete_blueprint(blueprint_id)
        session.flush()
        recompute(session, spec_ids=[spec_id])
        return DeleteResult(
            deleted_id=blueprint_id, specification_id=spec_id, project_id=project_id
        )


def delete_specification(
    session_factory: sessionmaker[Session], specification_id: str
) -> DeleteResult:
    """Delete a spec and its whole subtree, then recompute the PROJECT counts directly.

    The spec's project id is captured FIRST: once the spec row is gone the standard
    ``recompute(spec_ids=[deleted])`` cannot reach it, so the project's spec-bucket /
    child rollups are re-derived here from the project's surviving spec rows
    (closing the gap the executor flagged).
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        spec = stores.specifications.get_specification(specification_id)  # existence check
        project_id = spec.project_id
        stores.specifications.delete_specification(specification_id)
        session.flush()
        _recompute_project_counts(session, project_id)
        return DeleteResult(deleted_id=specification_id, specification_id=None, project_id=project_id)


def _spec_project_id(session: Session, spec_id: str | None) -> str | None:
    """The owning project id of a spec (``None`` when the spec id is/was ``None``)."""
    if spec_id is None:
        return None
    spec = session.get(Specification, spec_id)
    return spec.project_id if spec is not None else None


def _recompute_project_counts(session: Session, project_id: str) -> None:
    """Re-derive a project's spec-bucket + child rollups from its surviving specs.

    The direct project rollup the standard worklist runs as its tail — extracted
    here for :func:`delete_specification`, where the deleted spec can no longer seed
    ``recompute``. Mirrors ``recompute._recompute_project`` exactly.
    """
    project = session.get(Project, project_id)
    if project is None:
        return
    specs = list(
        session.execute(
            select(Specification).where(Specification.project_id == project_id)
        ).scalars()
    )
    views = [
        SpecCountsView(
            status=SpecStatus(s.status),
            epic_count=s.epic_count,
            completed_epic_count=s.completed_epic_count,
            ticket_count=s.ticket_count,
            completed_ticket_count=s.completed_ticket_count,
        )
        for s in specs
    ]
    pc = derive_project_counts(views)
    project.spec_count = pc.spec_count
    project.completed_spec_count = pc.completed_spec_count
    project.draft_spec_count = pc.draft_spec_count
    project.planning_spec_count = pc.planning_spec_count
    project.ready_spec_count = pc.ready_spec_count
    project.in_progress_spec_count = pc.in_progress_spec_count
    project.in_review_spec_count = pc.in_review_spec_count
    project.epic_count = pc.epic_count
    project.completed_epic_count = pc.completed_epic_count
    project.ticket_count = pc.ticket_count
    project.completed_ticket_count = pc.completed_ticket_count


# --------------------------------------------------------------------------- #
# dependencies                                                                 #
# --------------------------------------------------------------------------- #


def would_create_cycle(
    edges: Sequence[DependencyEdge], ticket_id: str, depends_on_id: str
) -> bool:
    """Would adding ``ticket_id → depends_on_id`` introduce a cycle?

    Verbatim port of the TS ``wouldCreateCycle`` BFS: follow the existing edge set
    forward from ``depends_on_id`` (each ``ticket → depends_on`` step); if it can
    already reach ``ticket_id`` then closing the new edge forms a cycle. A
    self-edge is trivially a cycle.
    """
    if ticket_id == depends_on_id:
        return True
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.ticket_id, []).append(edge.depends_on_id)

    visited: set[str] = set()
    queue: deque[str] = deque([depends_on_id])
    while queue:
        current = queue.popleft()
        if current == ticket_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        for nxt in adjacency.get(current, ()):
            if nxt not in visited:
                queue.append(nxt)
    return False


def _coerce_dep_type(value: DependencyType | str) -> DependencyType:
    """Coerce a dependency type to :class:`DependencyType`, raising on an unknown value."""
    if isinstance(value, DependencyType):
        return value
    try:
        return DependencyType(value)
    except ValueError as exc:
        raise ValidationFailedError(
            f"Invalid dependency type: {value!r} (expected 'requires' or 'blocks').", cause=exc
        ) from exc


def add_dependency(
    session_factory: sessionmaker[Session],
    ticket_id: str,
    depends_on_id: str,
    type: DependencyType | str = DependencyType.REQUIRES,
) -> TicketRecord:
    """Add a ``ticket_id → depends_on_id`` dependency edge and recompute the spec.

    Rejects, in order: a self-dependency (:class:`ValidationFailedError`), a
    cross-specification edge (:class:`ValidationFailedError`), a duplicate edge
    (:class:`ConflictError`), and an edge that would create a cycle
    (:class:`ConflictError`). Returns the (re-read) dependent ticket — its status
    reflects the post-add cascade.
    """
    dep_type = _coerce_dep_type(type)
    if ticket_id == depends_on_id:
        raise ValidationFailedError(
            f"Dependency rejected: ticketId and dependsOnId are the same ({ticket_id}).",
            context={"ticket_id": ticket_id, "depends_on_id": depends_on_id},
        )

    with session_factory.begin() as session:
        stores = make_stores(session)
        if stores.tickets.get_ticket(ticket_id) is None:
            raise NotFoundError(f"Ticket not found: {ticket_id}")
        if stores.tickets.get_ticket(depends_on_id) is None:
            raise NotFoundError(f"Ticket not found: {depends_on_id}")

        spec_id = _ticket_spec_id(session, ticket_id)
        dep_spec_id = _ticket_spec_id(session, depends_on_id)
        if spec_id is None or dep_spec_id is None or spec_id != dep_spec_id:
            raise ValidationFailedError(
                "Cross-specification dependencies are not allowed: "
                f"ticket {ticket_id} (spec {spec_id}) vs dependsOn {depends_on_id} "
                f"(spec {dep_spec_id}).",
                context={"ticket_id": ticket_id, "depends_on_id": depends_on_id},
            )

        edges = stores.ticket_dependencies.list_dependencies(specification_id=spec_id)
        if any(e.ticket_id == ticket_id and e.depends_on_id == depends_on_id for e in edges):
            raise ConflictError(
                f"Dependency from {ticket_id} -> {depends_on_id} already exists.",
                context={"ticket_id": ticket_id, "depends_on_id": depends_on_id},
            )
        if would_create_cycle(edges, ticket_id, depends_on_id):
            raise ConflictError(
                f"Adding {ticket_id} -> {depends_on_id} would create a cycle in the "
                "dependency graph.",
                context={"ticket_id": ticket_id, "depends_on_id": depends_on_id},
            )

        stores.ticket_dependencies.add_dependency(ticket_id, depends_on_id, dep_type)
        recompute(session, spec_ids=[spec_id])
        result = stores.tickets.get_ticket(ticket_id)
        assert result is not None  # existence checked above
        return result


def remove_dependency(
    session_factory: sessionmaker[Session],
    *,
    dependency_id: str | None = None,
    ticket_id: str | None = None,
    depends_on_id: str | None = None,
) -> TicketRecord | None:
    """Remove a dependency edge (by id, or by its ``(ticket_id, depends_on_id)`` pair).

    Recomputes the dependent ticket's spec (its status may promote ``pending →
    ready``). Returns the re-read dependent ticket, or ``None`` if it no longer
    exists.
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        if dependency_id is not None:
            row = session.get(TicketDependency, dependency_id)
            if row is None:
                raise NotFoundError(f"Dependency not found: {dependency_id}")
            dep_ticket_id = row.ticket_id
        elif ticket_id is not None and depends_on_id is not None:
            dep_ticket_id = ticket_id
        else:
            raise ValidationFailedError(
                "remove_dependency requires dependency_id or (ticket_id, depends_on_id)."
            )

        spec_id = _ticket_spec_id(session, dep_ticket_id)
        stores.ticket_dependencies.remove_dependency(
            dependency_id=dependency_id, ticket_id=ticket_id, depends_on_id=depends_on_id
        )
        recompute(session, spec_ids=_recompute_spec_ids([spec_id]))
        return stores.tickets.get_ticket(dep_ticket_id)


def bulk_add_dependencies(
    session_factory: sessionmaker[Session], items: Sequence[DependencyRequest]
) -> BulkAddResult:
    """Add up to 100 dependency edges with PER-ITEM partial success; recompute once.

    The batch is capped at :data:`MAX_BULK_DEPENDENCIES` (a batch over the cap is
    REJECTED outright, never silently truncated). There is NO cycle pre-check
    (faithful to the operations bulk path — run a cycle report afterward if needed);
    a self-edge, a duplicate ``(ticket, depends_on)`` and a bad FK each fail only
    THEIR item (isolated by a SAVEPOINT) and are collected in ``failed``. The
    recompute worklist runs once at the end over every touched spec.
    """
    if len(items) > MAX_BULK_DEPENDENCIES:
        raise ValidationFailedError(
            f"bulk_add_dependencies caps a single batch at {MAX_BULK_DEPENDENCIES} pairs "
            f"(received {len(items)}).",
            context={"count": len(items)},
        )

    failed: list[DependencyFailure] = []
    affected: set[str] = set()
    added = 0
    with session_factory.begin() as session:
        stores = make_stores(session)
        for item in items:
            if item.ticket_id == item.depends_on_id:
                failed.append(
                    DependencyFailure(
                        item.ticket_id,
                        item.depends_on_id,
                        f"ticketId and dependsOnId are the same ({item.ticket_id}).",
                    )
                )
                continue
            try:
                # SAVEPOINT per item: a constraint violation (duplicate / bad FK)
                # rolls back only this edge and leaves the batch transaction usable.
                with session.begin_nested():
                    stores.ticket_dependencies.add_dependency(
                        item.ticket_id, item.depends_on_id, item.type
                    )
            except CrudError as exc:
                failed.append(DependencyFailure(item.ticket_id, item.depends_on_id, str(exc)))
                continue
            added += 1
            spec_id = _ticket_spec_id(session, item.ticket_id)
            if spec_id is not None:
                affected.add(spec_id)

        recompute(session, spec_ids=affected)
    return BulkAddResult(added=added, failed=failed)


# --------------------------------------------------------------------------- #
# blueprint <-> ticket links                                                   #
# --------------------------------------------------------------------------- #


def link_blueprint_to_ticket(
    session_factory: sessionmaker[Session],
    ticket_id: str,
    blueprint_id: str,
    *,
    context: str | None = None,
    section: str | None = None,
) -> TicketRecord:
    """Link a blueprint to a ticket (``ticket_blueprint_refs``) and recompute the spec.

    Rejects a duplicate ``(ticket, blueprint)`` link with :class:`ConflictError`.
    Returns the re-read ticket (its ``blueprint_count`` reflects the new ref).
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        if stores.tickets.get_ticket(ticket_id) is None:
            raise NotFoundError(f"Ticket not found: {ticket_id}")
        stores.blueprints.get_blueprint(blueprint_id)  # existence check (raises NotFound)

        existing = session.execute(
            select(TicketBlueprintRef).where(
                TicketBlueprintRef.ticket_id == ticket_id,
                TicketBlueprintRef.blueprint_id == blueprint_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(
                f"Ticket {ticket_id} is already linked to blueprint {blueprint_id}.",
                context={"ticket_id": ticket_id, "blueprint_id": blueprint_id},
            )

        session.add(
            TicketBlueprintRef(
                ticket_id=ticket_id, blueprint_id=blueprint_id, context=context, section=section
            )
        )
        try:
            session.flush()
        except IntegrityError as exc:
            raise to_crud_error(exc, intent="add") from exc

        spec_id = _ticket_spec_id(session, ticket_id)
        recompute(session, spec_ids=_recompute_spec_ids([spec_id]))
        result = stores.tickets.get_ticket(ticket_id)
        assert result is not None  # existence checked above
        return result


def unlink_blueprint_from_ticket(
    session_factory: sessionmaker[Session],
    *,
    reference_id: str | None = None,
    ticket_id: str | None = None,
    blueprint_id: str | None = None,
) -> TicketRecord | None:
    """Unlink a blueprint from a ticket (by ref id, or by ``(ticket, blueprint)``).

    Recomputes the ticket's spec (its ``blueprint_count`` drops). Returns the
    re-read ticket, or ``None`` if it no longer exists.
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        if reference_id is not None:
            row = session.get(TicketBlueprintRef, reference_id)
        elif ticket_id is not None and blueprint_id is not None:
            row = session.execute(
                select(TicketBlueprintRef).where(
                    TicketBlueprintRef.ticket_id == ticket_id,
                    TicketBlueprintRef.blueprint_id == blueprint_id,
                )
            ).scalar_one_or_none()
        else:
            raise ValidationFailedError(
                "unlink_blueprint_from_ticket requires reference_id or (ticket_id, blueprint_id)."
            )
        if row is None:
            raise NotFoundError("Ticket-blueprint reference not found.")

        dep_ticket_id = row.ticket_id
        spec_id = _ticket_spec_id(session, dep_ticket_id)
        session.delete(row)
        session.flush()
        recompute(session, spec_ids=_recompute_spec_ids([spec_id]))
        return stores.tickets.get_ticket(dep_ticket_id)
