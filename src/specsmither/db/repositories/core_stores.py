"""``*StoreSqlite`` repositories for the core spine entities.

The SQLite store implementation set, drop-in via the same DI bag. Covers the five
entities whose array fields are stored
as JSON columns rather than child tables, so each ORM row maps **directly** to its
``domain.records`` DTO (no decompose / recompose):

* :class:`ProjectStoreSqlite`        — ``projects`` ⇄ :class:`ProjectRecord` (defined here)
* :class:`SpecStoreSqlite`           — ``specifications`` ⇄ ``SpecificationRecord``
* :class:`EpicStoreSqlite`           — ``epics`` ⇄ ``EpicRecord``
* :class:`BlueprintStoreSqlite`      — ``blueprints`` ⇄ ``BlueprintRecord`` (content inline)
* :class:`TicketDependencyStoreSqlite`— ``ticket_dependencies`` ⇄ ``DependencyEdge``

Two contract rules (architecture §4, invariants 3+4):

1. **Session-bound, no txn ownership.** Every store subclasses
   :class:`~specsmither.db.repositories.base.SessionStore` and runs inside the
   caller's ``Session.begin()``. Writes ``flush`` (to assign ULIDs / surface
   constraint violations) but NEVER ``commit`` / ``begin``.
2. **Count + derived columns are never written here.** The ``updateX*Count``
   delta-mutator family is implemented as no-ops; the denormalized counts, the
   cached ``dependency_tree``, the graph metrics, ``progress`` and (for epics)
   ``status`` are materialized only by the recompute worklist. Validator
   cache columns (``last_validator_output`` …) are read-through but written by the
   assay adapter, not here.

The projection variants (``listXForDashboard`` / ``ForReadiness`` / ``ForGateCheck``)
collapse into one ``get_x`` + one ``list_x`` — projection economy is moot in SQLite.
There is no auth (single local user); the only existence check is the
``None`` → :class:`~specsmither.operations.errors.NotFoundError` seam.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from crucible.models.enums import DependencyType
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from specsmither.db.models import (
    Blueprint,
    Epic,
    Project,
    Specification,
    Ticket,
    TicketDependency,
)
from specsmither.db.repositories.base import SessionStore, require_found
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
)
from specsmither.operations.errors import to_crud_error

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "BlueprintStoreSqlite",
    "EpicStoreSqlite",
    "ProjectRecord",
    "ProjectStoreSqlite",
    "SpecStoreSqlite",
    "TicketDependencyStoreSqlite",
]


# --------------------------------------------------------------------------- #
# row <-> DTO codec helpers                                                    #
# --------------------------------------------------------------------------- #


def _scalar(value: Any) -> Any:
    """Coerce an enum to its plain string ``value`` (passthrough for everything else)."""
    return value.value if isinstance(value, Enum) else value


def _json(value: Any) -> Any:
    """Serialize a record field to a JSON-primitive for a :class:`JSONType` column.

    Pydantic sub-models (and lists / dicts thereof — e.g. ``Scope``, ``list[Goal]``,
    ``dict[str, FieldDeclaration]``) become their camelCase wire dicts; primitives and
    ``None`` pass through. The inverse is implicit: :meth:`BaseModel.model_validate`
    coerces the stored dict back into the typed sub-model on read.
    """
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    return value


def _flush(session: Session, *, intent: str) -> None:
    """Flush the unit of work, translating a constraint violation to a ``CrudError``."""
    try:
        session.flush()
    except IntegrityError as exc:
        raise to_crud_error(exc, intent=intent) from exc


# --------------------------------------------------------------------------- #
# Project                                                                      #
# --------------------------------------------------------------------------- #


class ProjectRecord(BaseModel):
    """A ``projects`` row DTO.

    There is no ``ProjectRecord`` in :mod:`crucible` (projects are a SpecSmither
    container), so it is defined here following the same camelCase-alias convention
    as the other ``domain.records`` DTOs. ``progress`` is deliberately absent
    (project progress is derived read-side, never persisted). The count columns are
    read-through but recompute-owned.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
        frozen=True,
    )

    id: str
    user_id: str = "local"
    name: str
    description: str | None = None
    status: str | None = None

    spec_count: int = 0
    completed_spec_count: int = 0
    draft_spec_count: int = 0
    planning_spec_count: int = 0
    ready_spec_count: int = 0
    in_progress_spec_count: int = 0
    in_review_spec_count: int = 0
    epic_count: int = 0
    completed_epic_count: int = 0
    ticket_count: int = 0
    completed_ticket_count: int = 0

    created_at: str | None = None
    updated_at: str | None = None


_PROJECT_COLUMNS = (
    "id",
    "user_id",
    "name",
    "description",
    "status",
    "spec_count",
    "completed_spec_count",
    "draft_spec_count",
    "planning_spec_count",
    "ready_spec_count",
    "in_progress_spec_count",
    "in_review_spec_count",
    "epic_count",
    "completed_epic_count",
    "ticket_count",
    "completed_ticket_count",
    "created_at",
    "updated_at",
)


def _project_to_record(obj: Project) -> ProjectRecord:
    return ProjectRecord.model_validate({c: getattr(obj, c) for c in _PROJECT_COLUMNS})


def _apply_project(obj: Project, rec: ProjectRecord, *, creating: bool) -> None:
    if creating:
        obj.user_id = rec.user_id
    obj.name = rec.name
    obj.description = rec.description
    obj.status = _scalar(rec.status)


class ProjectStoreSqlite(SessionStore):
    """``projects`` CRUD. Counts are read-through but written only by recompute."""

    def get_project(self, project_id: str) -> ProjectRecord:
        obj = require_found(
            self.session.get(Project, project_id), kind="project", entity_id=project_id
        )
        return _project_to_record(obj)

    def list_projects(
        self, *, user_id: str | None = None, limit: int | None = None
    ) -> list[ProjectRecord]:
        stmt = select(Project)
        if user_id is not None:
            stmt = stmt.where(Project.user_id == user_id)
        stmt = stmt.order_by(Project.created_at, Project.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [_project_to_record(o) for o in self.session.execute(stmt).scalars().all()]

    def create_project(self, record: ProjectRecord) -> ProjectRecord:
        obj = Project()
        if record.id:
            obj.id = record.id
        _apply_project(obj, record, creating=True)
        self.session.add(obj)
        _flush(self.session, intent="create")
        return _project_to_record(obj)

    def update_project(self, record: ProjectRecord) -> ProjectRecord:
        obj = require_found(
            self.session.get(Project, record.id), kind="project", entity_id=record.id
        )
        _apply_project(obj, record, creating=False)
        _flush(self.session, intent="update")
        return _project_to_record(obj)

    def delete_project(self, project_id: str) -> None:
        obj = require_found(
            self.session.get(Project, project_id), kind="project", entity_id=project_id
        )
        self.session.delete(obj)
        _flush(self.session, intent="delete")

    # --- aggregator-only delta-mutators -> no-ops (counts owned by recompute) ---
    def update_project_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_completed_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_draft_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_planning_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_ready_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_in_progress_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_in_review_spec_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_epic_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_completed_epic_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_ticket_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_completed_ticket_count(self, project_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_project_member_count(self, project_id: str, delta: int) -> None:
        """No-op: sharing is dropped (single local user); no ``member_count`` column."""


# --------------------------------------------------------------------------- #
# Specification                                                                #
# --------------------------------------------------------------------------- #


_SPEC_COLUMNS = (
    "id",
    "project_id",
    "title",
    "specification_type_id",
    "schema_version",
    "description",
    "background",
    "status",
    "progress",
    "architecture",
    "scope",
    "goals",
    "requirements",
    "non_functional_requirements",
    "acceptance_criteria",
    "guardrails",
    "tech_stack",
    "folder_structures",
    "shared_patterns",
    "epic_targets",
    "field_declarations",
    "tags",
    "estimated_minutes",
    "epic_count",
    "completed_epic_count",
    "todo_epic_count",
    "in_progress_epic_count",
    "ticket_count",
    "completed_ticket_count",
    "pending_ticket_count",
    "ready_ticket_count",
    "active_ticket_count",
    "dependency_count",
    "blueprint_count",
    "dependency_tree",
    "dependency_tree_version",
    "dependency_tree_updated_at",
    "critical_path_length",
    "max_blocker_depth",
    "last_validator_output",
    "last_gate_result",
    "last_validated_at",
    "created_at",
    "updated_at",
)


def _spec_to_record(obj: Specification) -> SpecificationRecord:
    return SpecificationRecord.model_validate({c: getattr(obj, c) for c in _SPEC_COLUMNS})


def _apply_spec(obj: Specification, rec: SpecificationRecord, *, creating: bool) -> None:
    if creating:
        obj.project_id = rec.project_id
    obj.specification_type_id = rec.specification_type_id
    obj.title = rec.title
    obj.description = rec.description
    obj.background = rec.background
    obj.status = _scalar(rec.status)
    obj.schema_version = rec.schema_version
    obj.architecture = rec.architecture or ""
    obj.scope = _json(rec.scope)
    obj.goals = _json(rec.goals)
    obj.requirements = _json(rec.requirements)
    obj.non_functional_requirements = _json(rec.non_functional_requirements)
    obj.acceptance_criteria = _json(rec.acceptance_criteria)
    obj.guardrails = _json(rec.guardrails)
    obj.tech_stack = _json(rec.tech_stack)
    obj.folder_structures = _json(rec.folder_structures)
    obj.shared_patterns = _json(rec.shared_patterns)
    obj.epic_targets = _json(rec.epic_targets)
    obj.field_declarations = _json(rec.field_declarations)
    obj.tags = _json(rec.tags)


class SpecStoreSqlite(SessionStore):
    """``specifications`` CRUD. JSON content columns round-trip; counts / tree /
    graph-metrics / validator-cache columns are read-through but recompute-owned."""

    def get_specification(self, spec_id: str) -> SpecificationRecord:
        obj = require_found(
            self.session.get(Specification, spec_id), kind="specification", entity_id=spec_id
        )
        return _spec_to_record(obj)

    def list_specifications(
        self, *, project_id: str | None = None, limit: int | None = None
    ) -> list[SpecificationRecord]:
        stmt = select(Specification)
        if project_id is not None:
            stmt = stmt.where(Specification.project_id == project_id)
        stmt = stmt.order_by(Specification.created_at, Specification.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [_spec_to_record(o) for o in self.session.execute(stmt).scalars().all()]

    def create_specification(self, record: SpecificationRecord) -> SpecificationRecord:
        obj = Specification()
        if record.id:
            obj.id = record.id
        _apply_spec(obj, record, creating=True)
        self.session.add(obj)
        _flush(self.session, intent="create")
        return _spec_to_record(obj)

    def update_specification(self, record: SpecificationRecord) -> SpecificationRecord:
        obj = require_found(
            self.session.get(Specification, record.id),
            kind="specification",
            entity_id=record.id,
        )
        _apply_spec(obj, record, creating=False)
        _flush(self.session, intent="update")
        return _spec_to_record(obj)

    def delete_specification(self, spec_id: str) -> None:
        obj = require_found(
            self.session.get(Specification, spec_id), kind="specification", entity_id=spec_id
        )
        self.session.delete(obj)
        _flush(self.session, intent="delete")

    # --- aggregator-only delta-mutators -> no-ops (owned by recompute) ---
    def update_specification_epic_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_todo_epic_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_in_progress_epic_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_completed_epic_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_ticket_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_completed_ticket_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_pending_ticket_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_ready_ticket_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_active_ticket_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_estimated_minutes(self, spec_id: str, delta: int) -> None:
        """No-op: estimated minutes are derived by the recompute worklist."""

    def update_specification_dependency_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_blueprint_count(self, spec_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_specification_progress(self, spec_id: str, progress: int) -> None:
        """No-op: ``progress`` is derived by the recompute worklist."""


# --------------------------------------------------------------------------- #
# Epic                                                                         #
# --------------------------------------------------------------------------- #


_EPIC_COLUMNS = (
    "id",
    "specification_id",
    "title",
    "description",
    "objective",
    "epic_number",
    "order",
    "status",
    "planning_type",
    "progress",
    "category",
    "architecture",
    "scope",
    "goals",
    "acceptance_criteria",
    "validation_commands",
    "api_contracts",
    "shared_patterns",
    "file_structures",
    "requirements_covered",
    "nfrs_covered",
    "goals_covered",
    "field_declarations",
    "ticket_count",
    "completed_ticket_count",
    "pending_ticket_count",
    "ready_ticket_count",
    "active_ticket_count",
    "dependency_count",
    "blueprint_count",
    "estimated_minutes",
    "tags",
    "created_at",
    "updated_at",
)


def _epic_to_record(obj: Epic) -> EpicRecord:
    return EpicRecord.model_validate({c: getattr(obj, c) for c in _EPIC_COLUMNS})


def _apply_epic(obj: Epic, rec: EpicRecord, *, creating: bool) -> None:
    if creating:
        obj.specification_id = rec.specification_id
    # ``epic_number`` is NOT NULL; a missing value surfaces as a flush-time
    # ValidationFailedError (the operations layer assigns ``max + 1``).
    if rec.epic_number is not None:
        obj.epic_number = rec.epic_number
    obj.title = rec.title
    obj.description = rec.description
    obj.objective = rec.objective
    obj.order = rec.order
    obj.category = _scalar(rec.category)
    obj.planning_type = rec.planning_type
    obj.architecture = rec.architecture
    obj.scope = _json(rec.scope)
    obj.goals = _json(rec.goals)
    obj.acceptance_criteria = _json(rec.acceptance_criteria)
    obj.validation_commands = _json(rec.validation_commands)
    obj.api_contracts = _json(rec.api_contracts)
    obj.shared_patterns = _json(rec.shared_patterns)
    obj.file_structures = _json(rec.file_structures)
    obj.requirements_covered = _json(rec.requirements_covered)
    obj.nfrs_covered = _json(rec.nfrs_covered)
    obj.goals_covered = _json(rec.goals_covered)
    obj.field_declarations = _json(rec.field_declarations)
    obj.tags = _json(rec.tags)


class EpicStoreSqlite(SessionStore):
    """``epics`` CRUD. ``status`` / ``progress`` / counts are recompute-owned."""

    def get_epic(self, epic_id: str) -> EpicRecord:
        obj = require_found(self.session.get(Epic, epic_id), kind="epic", entity_id=epic_id)
        return _epic_to_record(obj)

    def list_epics(
        self, *, specification_id: str | None = None, limit: int | None = None
    ) -> list[EpicRecord]:
        stmt = select(Epic)
        if specification_id is not None:
            stmt = stmt.where(Epic.specification_id == specification_id)
        stmt = stmt.order_by(Epic.order.is_(None), Epic.order, Epic.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [_epic_to_record(o) for o in self.session.execute(stmt).scalars().all()]

    def create_epic(self, record: EpicRecord) -> EpicRecord:
        obj = Epic()
        if record.id:
            obj.id = record.id
        _apply_epic(obj, record, creating=True)
        self.session.add(obj)
        _flush(self.session, intent="create")
        return _epic_to_record(obj)

    def update_epic(self, record: EpicRecord) -> EpicRecord:
        obj = require_found(self.session.get(Epic, record.id), kind="epic", entity_id=record.id)
        _apply_epic(obj, record, creating=False)
        _flush(self.session, intent="update")
        return _epic_to_record(obj)

    def delete_epic(self, epic_id: str) -> None:
        obj = require_found(self.session.get(Epic, epic_id), kind="epic", entity_id=epic_id)
        self.session.delete(obj)
        _flush(self.session, intent="delete")

    # --- aggregator-only delta-mutators -> no-ops (owned by recompute) ---
    def update_epic_ticket_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_pending_ticket_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_ready_ticket_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_active_ticket_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_completed_ticket_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_estimated_minutes(self, epic_id: str, delta: int) -> None:
        """No-op: estimated minutes are derived by the recompute worklist."""

    def update_epic_dependency_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_blueprint_count(self, epic_id: str, delta: int) -> None:
        """No-op: count columns are written only by the recompute worklist."""

    def update_epic_computed_fields(
        self, epic_id: str, *, status: str | None = None, progress: int | None = None
    ) -> None:
        """No-op: ``status`` / ``progress`` are derived by the recompute worklist."""


# --------------------------------------------------------------------------- #
# Blueprint                                                                    #
# --------------------------------------------------------------------------- #


_BLUEPRINT_COLUMNS = (
    "id",
    "specification_id",
    "title",
    "category",
    "description",
    "slug",
    "format",
    "coverage_type",
    "content",
    "notes",
    "version",
    "order",
    "status",
    "tags",
    "created_at",
    "updated_at",
)


def _blueprint_to_record(obj: Blueprint) -> BlueprintRecord:
    return BlueprintRecord.model_validate({c: getattr(obj, c) for c in _BLUEPRINT_COLUMNS})


def _apply_blueprint(obj: Blueprint, rec: BlueprintRecord, *, creating: bool) -> None:
    if creating:
        obj.specification_id = rec.specification_id
    obj.category = _scalar(rec.category)
    obj.title = rec.title
    obj.description = rec.description
    obj.slug = rec.slug
    obj.format = _scalar(rec.format)
    obj.coverage_type = _scalar(rec.coverage_type)
    obj.content = rec.content
    obj.notes = rec.notes
    obj.version = rec.version
    obj.order = rec.order
    # ``status`` is NOT NULL (default ``"draft"``); only overwrite on an explicit value.
    if rec.status is not None:
        obj.status = _scalar(rec.status)
    obj.tags = _json(rec.tags)


class BlueprintStoreSqlite(SessionStore):
    """``blueprints`` CRUD with the body stored inline (``content``)."""

    def get_blueprint(self, blueprint_id: str) -> BlueprintRecord:
        obj = require_found(
            self.session.get(Blueprint, blueprint_id), kind="blueprint", entity_id=blueprint_id
        )
        return _blueprint_to_record(obj)

    def list_blueprints(
        self, *, specification_id: str | None = None, limit: int | None = None
    ) -> list[BlueprintRecord]:
        stmt = select(Blueprint)
        if specification_id is not None:
            stmt = stmt.where(Blueprint.specification_id == specification_id)
        stmt = stmt.order_by(Blueprint.order.is_(None), Blueprint.order, Blueprint.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [_blueprint_to_record(o) for o in self.session.execute(stmt).scalars().all()]

    def create_blueprint(self, record: BlueprintRecord) -> BlueprintRecord:
        obj = Blueprint()
        if record.id:
            obj.id = record.id
        _apply_blueprint(obj, record, creating=True)
        self.session.add(obj)
        _flush(self.session, intent="create")
        return _blueprint_to_record(obj)

    def update_blueprint(self, record: BlueprintRecord) -> BlueprintRecord:
        obj = require_found(
            self.session.get(Blueprint, record.id), kind="blueprint", entity_id=record.id
        )
        _apply_blueprint(obj, record, creating=False)
        _flush(self.session, intent="update")
        return _blueprint_to_record(obj)

    def delete_blueprint(self, blueprint_id: str) -> None:
        obj = require_found(
            self.session.get(Blueprint, blueprint_id), kind="blueprint", entity_id=blueprint_id
        )
        self.session.delete(obj)
        _flush(self.session, intent="delete")


# --------------------------------------------------------------------------- #
# TicketDependency                                                             #
# --------------------------------------------------------------------------- #


_DEP_COLUMNS = ("id", "ticket_id", "depends_on_id", "type", "created_at")


def _dep_to_edge(obj: TicketDependency) -> DependencyEdge:
    return DependencyEdge.model_validate({c: getattr(obj, c) for c in _DEP_COLUMNS})


class TicketDependencyStoreSqlite(SessionStore):
    """``ticket_dependencies`` edges (directed ``ticket → depends_on``).

    No cycle check here — that lives in the CRUD ``add_dependency`` verb. The
    ``UNIQUE(ticket_id, depends_on_id)`` collision is mapped to a ``ConflictError``.
    """

    def add_dependency(
        self,
        ticket_id: str,
        depends_on_id: str,
        type: DependencyType | str = DependencyType.REQUIRES,
    ) -> DependencyEdge:
        obj = TicketDependency(
            ticket_id=ticket_id,
            depends_on_id=depends_on_id,
            type=_scalar(type),
        )
        self.session.add(obj)
        # ``add`` intent: a duplicate edge is a CONFLICT (a bad FK is VALIDATION_FAILED).
        _flush(self.session, intent="add")
        return _dep_to_edge(obj)

    def remove_dependency(
        self,
        *,
        dependency_id: str | None = None,
        ticket_id: str | None = None,
        depends_on_id: str | None = None,
    ) -> None:
        """Remove an edge by id, or by its ``(ticket_id, depends_on_id)`` pair."""
        if dependency_id is not None:
            row = self.session.get(TicketDependency, dependency_id)
            ident = dependency_id
        elif ticket_id is not None and depends_on_id is not None:
            row = self.session.execute(
                select(TicketDependency).where(
                    TicketDependency.ticket_id == ticket_id,
                    TicketDependency.depends_on_id == depends_on_id,
                )
            ).scalar_one_or_none()
            ident = f"{ticket_id}->{depends_on_id}"
        else:
            raise ValueError(
                "remove_dependency requires dependency_id or (ticket_id, depends_on_id)"
            )
        edge = require_found(row, kind="ticket_dependency", entity_id=ident)
        self.session.delete(edge)
        _flush(self.session, intent="delete")

    def list_dependencies(
        self, *, ticket_id: str | None = None, specification_id: str | None = None
    ) -> list[DependencyEdge]:
        """Edges for one ticket, for a whole spec (the recompute neighborhood), or all.

        The spec-wide variant joins ``ticket_dependencies → tickets → epics`` and
        keeps every edge whose dependent ticket belongs to ``specification_id``.
        """
        stmt = select(TicketDependency)
        if specification_id is not None:
            stmt = (
                stmt.join(Ticket, TicketDependency.ticket_id == Ticket.id)
                .join(Epic, Ticket.epic_id == Epic.id)
                .where(Epic.specification_id == specification_id)
            )
        elif ticket_id is not None:
            stmt = stmt.where(TicketDependency.ticket_id == ticket_id)
        stmt = stmt.order_by(TicketDependency.ticket_id, TicketDependency.depends_on_id)
        return [_dep_to_edge(o) for o in self.session.execute(stmt).scalars().all()]
