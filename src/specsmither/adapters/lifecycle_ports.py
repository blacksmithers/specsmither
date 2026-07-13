"""The coupled lifecycle-ports seam — concrete stores over the M0 SQLite layer.

This is the COUPLED surface the planning lifecycle is wired through (A1 §5): the
five :class:`~specsmither.lifecycle.ports.LifecyclePorts` slots, each a thin
session-bound adapter over the M0 stores / crucible validator / in-memory
projector / WritePlan executor, plus the :func:`make_lifecycle_ports` factory that
binds them all to **one** :class:`~sqlalchemy.orm.Session` (one ``BEGIN IMMEDIATE``
write lock per mutation — architecture invariant 3).

What lives here (everything else the lifecycle imports is already PURE):

* :class:`SqlitePlanningSessionStore` — :class:`~specsmither.lifecycle.ports.PlanningSessionStore`
  over the M0 read-only ``planning_stores.PlanningSessionStore`` repo. The repo's
  by-id ``get`` raises ``NotFoundError`` (the existence seam), but the port contract
  is ``| None``, so the missing-row case is folded back to ``None``; the
  by-project listing (which the M0 repo does not expose — sessions carry no
  ``project_id``) is a direct ``planning_sessions ⋈ specifications`` join.
* :class:`SqliteSpecStore` — :class:`~specsmither.lifecycle.ports.SpecStore`.
  ``get_spec`` is the light single-item read (the spec entity recomposed with no
  children, exactly what the SPS status/config check needs); ``get_spec_full``
  loads every child table via the M0 stores, recomposes the nested crucible
  :class:`~crucible.models.Specification` via
  :func:`~specsmither.domain.records.build_spec_full`, and assembles the flat
  ``epics`` / ``blueprints`` / ``dependencies`` side-projections the pre-checks read.
* :class:`ProjectorOperationsLayer` — :class:`~specsmither.lifecycle.ports.OperationsLayer`.
  Bridges the lifecycle's camelCase wire ops to the M0 ``MutationOp`` union, applies
  them through the pure in-memory projector (no DB), and rebuilds a fresh
  :class:`~specsmither.lifecycle.ports.SpecFull` from the projected nested model.

``ConfigStoreSqlite`` (config seam) and ``CrucibleValidatorAdapter`` (crucible seam)
are already built (work items #5/#7) and are bound straight through by the factory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from crucible.models import Specification
from crucible.models.enums import BlueprintCategory
from sqlalchemy import select

from specsmither.adapters.crucible_validator import CrucibleValidatorAdapter
from specsmither.adapters.in_memory_operations import (
    CreateBlueprint,
    CreateDependencies,
    CreateEpic,
    CreateTicket,
    DeleteBlueprint,
    DeleteEpic,
    DeleteTicket,
    DependencyEdgeSpec,
    LinkBlueprintToTickets,
    MutationOp,
    UnlinkBlueprintFromTickets,
    UpdateBlueprint,
    UpdateEpic,
    UpdateSpec,
    UpdateTicket,
)
from specsmither.adapters.in_memory_operations import apply_mutation as project_mutation
from specsmither.adapters.write_plan_executor import WritePlan, apply_write_plan, to_snake
from specsmither.db.base import new_ulid
from specsmither.db.models import Epic as EpicRow
from specsmither.db.models import PlanningSession
from specsmither.db.models import Specification as SpecificationRow
from specsmither.db.models import Ticket as TicketRow
from specsmither.db.models import TicketBlueprintRef as TicketBlueprintRefRow
from specsmither.db.repositories import make_stores
from specsmither.db.repositories.config_store import ConfigStoreSqlite
from specsmither.db.repositories.planning_stores import PlanningSessionStore as PlanningSessionRepo
from specsmither.domain.records import (
    BlueprintRecord,
    EpicRecord,
    TicketRecord,
    build_spec_full,
)
from specsmither.domain.records import (
    TicketBlueprintRef as TicketBlueprintRefRecord,
)
from specsmither.lifecycle.ports import (
    BlueprintRef,
    EpicFull,
    LifecyclePorts,
    SpecDependencyEdge,
    SpecFull,
    TicketRef,
)
from specsmither.lifecycle.session_record import PlanningSessionRecord
from specsmither.operations.errors import NotFoundError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session

    from specsmither.lifecycle.ports import (
        Clock,
        IdGenerator,
    )
    from specsmither.lifecycle.ports import (
        OperationsLayer as _OperationsLayerProto,
    )
    from specsmither.lifecycle.ports import (
        PlanningSessionStore as _PlanningSessionStoreProto,
    )
    from specsmither.lifecycle.ports import (
        SpecStore as _SpecStoreProto,
    )

__all__ = [
    "ProjectorOperationsLayer",
    "SqlitePlanningSessionStore",
    "SqliteSpecStore",
    "make_lifecycle_ports",
]


# --------------------------------------------------------------------------- #
# small coercion helpers                                                       #
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    """The default :class:`~specsmither.lifecycle.ports.Clock` — current UTC instant."""
    return datetime.now(tz=UTC)


def _enum_str(value: Any) -> str:
    """Coerce an enum member to its plain ``.value`` string (passthrough for ``str``)."""
    return value.value if isinstance(value, Enum) else str(value)


def _record_extra(record: Any) -> dict[str, Any]:
    """The full record as a camelCase wire dict — the flat-view ``extra`` bag.

    The pre-checks read this opaquely (``EpicFull.extra`` powers the cross-cut
    referrer scan's ``JSON.stringify`` over the whole epic row), so the entire
    record is dumped, not just the typed columns.
    """
    dump: dict[str, Any] = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dump


# --------------------------------------------------------------------------- #
# flat-view builders (records → ports projections, and nested model → ports)   #
# --------------------------------------------------------------------------- #


def _ticket_ref_from_record(record: TicketRecord) -> TicketRef:
    return TicketRef(
        id=record.id,
        epic_id=record.epic_id,
        title=record.title,
        description=record.description,
        status=_enum_str(record.status),
        extra=_record_extra(record),
    )


def _epic_full_from_record(record: EpicRecord, tickets: list[TicketRef]) -> EpicFull:
    return EpicFull(
        id=record.id,
        specification_id=record.specification_id,
        title=record.title,
        tickets=tickets,
        description=record.description,
        extra=_record_extra(record),
    )


def _blueprint_ref_from_record(record: BlueprintRecord) -> BlueprintRef:
    return BlueprintRef(
        id=record.id,
        specification_id=record.specification_id,
        title=record.title,
        category=_enum_str(record.category),
        extra=_record_extra(record),
    )


def _spec_full_from_spec(spec: Specification) -> SpecFull:
    """Re-derive the flat ports views from a nested crucible :class:`Specification`.

    The projector's output: ``spec`` is the projected (post-mutation) source of
    truth; the flat ``epics`` / ``blueprints`` / ``dependencies`` are re-derived from
    its tree (a :class:`~crucible.models.DependencyLink` ``ticket_id == Y`` on ticket
    ``X`` is the edge ``from=X depends on to=Y``). No DB, no record status (the nested
    authoring model carries none — irrelevant to the validator/gate this feeds).
    """
    epics_flat: list[EpicFull] = []
    dependencies_flat: list[SpecDependencyEdge] = []
    for epic in spec.epics:
        tickets_flat: list[TicketRef] = []
        for ticket in epic.tickets:
            tickets_flat.append(
                TicketRef(
                    id=ticket.id,
                    epic_id=ticket.epic_id,
                    title=ticket.title,
                    description=ticket.description,
                )
            )
            for link in ticket.dependencies:
                dependencies_flat.append(
                    SpecDependencyEdge(from_ticket_id=ticket.id, to_ticket_id=link.ticket_id)
                )
        epics_flat.append(
            EpicFull(
                id=epic.id,
                specification_id=epic.specification_id,
                title=epic.title,
                tickets=tickets_flat,
                description=epic.description,
            )
        )
    blueprints_flat = [
        BlueprintRef(
            id=blueprint.id,
            specification_id=spec.id,
            title=blueprint.title,
            category=_enum_str(blueprint.category),
        )
        for blueprint in spec.blueprints
    ]
    return SpecFull(
        spec=spec,
        epics=epics_flat,
        blueprints=blueprints_flat,
        dependencies=dependencies_flat,
    )


# --------------------------------------------------------------------------- #
# PlanningSessionStore                                                          #
# --------------------------------------------------------------------------- #


def _session_record_from_orm(row: PlanningSession) -> PlanningSessionRecord:
    """Map an ORM ``PlanningSession`` row to the pure :class:`PlanningSessionRecord`.

    The record mirrors the row field-for-field for everything the pure verb surface
    reads, so this is a straight attribute copy — the boundary at which sqlalchemy
    stops and the DB-free lifecycle path begins.
    """
    return PlanningSessionRecord(
        id=row.id,
        specification_id=row.specification_id,
        status=row.status,
        current_phase=row.current_phase,
        started_at=row.started_at,
        last_action_at=row.last_action_at,
        completed_at=row.completed_at,
        closed_at=row.closed_at,
        last_score=row.last_score,
        actions_count=row.actions_count,
        pending_human_feedback=row.pending_human_feedback,
        last_transition_trigger=row.last_transition_trigger,
        last_transition_at=row.last_transition_at,
        last_read_at=row.last_read_at,
        last_validated_at=row.last_validated_at,
        last_gate_result=row.last_gate_result,
        last_validator_output=row.last_validator_output,
        last_process_guidance=row.last_process_guidance,
        last_lifecycle_planning_guidance=row.last_lifecycle_planning_guidance,
        session_metadata=row.session_metadata,
    )


class SqlitePlanningSessionStore:
    """:class:`~specsmither.lifecycle.ports.PlanningSessionStore` over the M0 repo.

    Session-bound: every method runs inside the caller's transaction. Delegates to
    the read-only ``planning_stores.PlanningSessionStore`` repo, adapting its
    raise-on-missing ``get`` to the port's ``| None`` contract and adding the
    by-project listing (a ``planning_sessions ⋈ specifications`` join — sessions
    carry no denormalized ``project_id``). Every ORM row crosses out to the pure
    :class:`PlanningSessionRecord` here (via :func:`_session_record_from_orm`) so no
    ORM instance ever reaches the DB-free verb surface.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = PlanningSessionRepo(session)

    def get_planning_session(self, session_id: str) -> PlanningSessionRecord | None:
        try:
            row = self._repo.get_planning_session(session_id)
        except NotFoundError:
            return None
        return _session_record_from_orm(row)

    def get_active_planning_session_by_spec(self, spec_id: str) -> PlanningSessionRecord | None:
        row = self._repo.get_active_for_spec(spec_id)
        return _session_record_from_orm(row) if row is not None else None

    def list_planning_sessions_by_spec(self, spec_id: str) -> list[PlanningSessionRecord]:
        rows = self._repo.list_planning_sessions(specification_id=spec_id)
        return [_session_record_from_orm(row) for row in rows]

    def list_planning_sessions_by_project(self, project_id: str) -> list[PlanningSessionRecord]:
        stmt = (
            select(PlanningSession)
            .join(SpecificationRow, PlanningSession.specification_id == SpecificationRow.id)
            .where(SpecificationRow.project_id == project_id)
            .order_by(PlanningSession.id.desc())
        )
        rows = self._session.execute(stmt).scalars()
        return [_session_record_from_orm(row) for row in rows]


# --------------------------------------------------------------------------- #
# SpecStore                                                                     #
# --------------------------------------------------------------------------- #


class SqliteSpecStore:
    """:class:`~specsmither.lifecycle.ports.SpecStore` over the M0 entity stores.

    Session-bound. ``get_spec`` recomposes the spec entity with no children (the
    light single-item read); ``get_spec_full`` loads the whole subtree and produces
    both the nested crucible :class:`~crucible.models.Specification` (the validator's
    scoring input) and the flat side-projections (the pre-checks' input).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._stores = make_stores(session)

    def get_spec(self, spec_id: str) -> Specification | None:
        try:
            spec_record = self._stores.specifications.get_specification(spec_id)
        except NotFoundError:
            return None
        # Single-item read: the spec entity with empty child arrays (the SPS status /
        # config check never inspects children — that is get_spec_full's job).
        return build_spec_full(spec_record, [], [], [], [])

    def get_spec_full(self, spec_id: str) -> SpecFull | None:
        try:
            spec_record = self._stores.specifications.get_specification(spec_id)
        except NotFoundError:
            return None

        epic_records = self._stores.epics.list_epics(specification_id=spec_id)
        ticket_records: list[TicketRecord] = []
        for epic_record in epic_records:
            ticket_records.extend(self._stores.tickets.list_tickets(epic_id=epic_record.id))
        blueprint_records = self._stores.blueprints.list_blueprints(specification_id=spec_id)
        dependency_edges = self._stores.ticket_dependencies.list_dependencies(
            specification_id=spec_id
        )
        blueprint_refs = self._load_blueprint_refs(spec_id)

        spec = build_spec_full(
            spec_record,
            epic_records,
            ticket_records,
            blueprint_records,
            dependency_edges,
            blueprint_refs=blueprint_refs,
        )

        tickets_by_epic: dict[str, list[TicketRef]] = {}
        for ticket_record in ticket_records:
            tickets_by_epic.setdefault(ticket_record.epic_id, []).append(
                _ticket_ref_from_record(ticket_record)
            )
        epics_flat = [
            _epic_full_from_record(epic_record, tickets_by_epic.get(epic_record.id, []))
            for epic_record in epic_records
        ]
        blueprints_flat = [_blueprint_ref_from_record(record) for record in blueprint_records]
        # records.DependencyEdge: ``ticket_id`` is the dependent, ``depends_on_id`` the
        # depended-on → ports edge ``from depends on to``.
        dependencies_flat = [
            SpecDependencyEdge(from_ticket_id=edge.ticket_id, to_ticket_id=edge.depends_on_id)
            for edge in dependency_edges
        ]

        return SpecFull(
            spec=spec,
            epics=epics_flat,
            blueprints=blueprints_flat,
            dependencies=dependencies_flat,
        )

    def _load_blueprint_refs(self, spec_id: str) -> list[TicketBlueprintRefRecord]:
        """The ticket↔blueprint join rows for a spec (``ticket_blueprint_refs ⋈ tickets ⋈ epics``).

        The M0 store bag exposes no dedicated reader for the join table, so this is a
        direct ORM query; the rows hydrate each ticket's ``blueprint_references`` in
        :func:`~specsmither.domain.records.build_spec_full`.
        """
        stmt = (
            select(TicketBlueprintRefRow)
            .join(TicketRow, TicketBlueprintRefRow.ticket_id == TicketRow.id)
            .join(EpicRow, TicketRow.epic_id == EpicRow.id)
            .where(EpicRow.specification_id == spec_id)
        )
        return [
            TicketBlueprintRefRecord(
                id=row.id,
                ticket_id=row.ticket_id,
                blueprint_id=row.blueprint_id,
                context=row.context,
                section=row.section,
                created_at=row.created_at,
            )
            for row in self._session.execute(stmt).scalars()
        ]


# --------------------------------------------------------------------------- #
# OperationsLayer                                                               #
# --------------------------------------------------------------------------- #


def _snake_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of an update ``fields`` map with camelCase wire keys snake-cased.

    The M0 ``MutationOp`` update dataclasses key ``fields`` by the snake_case crucible
    attribute name (``setattr`` lands on the real field); the lifecycle wire payload
    is camelCase, so the keys are normalised (``to_snake`` is idempotent on snake input).
    Values pass through untouched (re-validated when crucible re-parses the projected spec).
    """
    return {to_snake(str(key)): value for key, value in raw.items()}


def _build_mutation_op(op: str, payload: Mapping[str, Any]) -> MutationOp | None:
    """Map a lifecycle op name + camelCase payload to its :data:`MutationOp`, or ``None``.

    ``None`` means "no faithful in-memory projection": ``delete_dependencies`` carries
    opaque edge ids (``dependencyIds``) the nested tree cannot resolve to ``(from, to)``
    pairs — the projection is a no-op, matching the SpecForge projector's
    unchanged-on-remove behaviour. An unknown op name is likewise a no-op.
    """
    if op == "update_spec":
        return UpdateSpec(fields=_snake_fields(payload.get("fields", {})))
    if op == "create_epic":
        return CreateEpic(
            title=payload["title"],
            description=payload.get("description") or "",
            id=payload.get("id"),
        )
    if op == "update_epic":
        return UpdateEpic(id=payload["id"], fields=_snake_fields(payload.get("fields", {})))
    if op == "delete_epic":
        return DeleteEpic(id=payload["id"])
    if op == "create_ticket":
        return CreateTicket(
            epic_id=payload["epicId"],
            title=payload["title"],
            description=payload.get("description") or "",
            id=payload.get("id"),
        )
    if op == "update_ticket":
        return UpdateTicket(id=payload["id"], fields=_snake_fields(payload.get("fields", {})))
    if op == "delete_ticket":
        return DeleteTicket(id=payload["id"])
    if op == "create_blueprint":
        return CreateBlueprint(
            title=payload["title"],
            category=BlueprintCategory(payload["category"]),
            content=payload.get("content") or "",
            id=payload.get("id"),
        )
    if op == "update_blueprint":
        return UpdateBlueprint(id=payload["id"], fields=_snake_fields(payload.get("fields", {})))
    if op == "delete_blueprint":
        return DeleteBlueprint(id=payload["id"])
    if op == "link_blueprint_to_tickets":
        return LinkBlueprintToTickets(
            blueprint_id=payload["blueprintId"],
            ticket_ids=list(payload.get("ticketIds", [])),
        )
    if op == "unlink_blueprint_to_tickets":
        return UnlinkBlueprintFromTickets(
            blueprint_id=payload["blueprintId"],
            ticket_ids=list(payload.get("ticketIds", [])),
        )
    if op == "create_dependencies":
        return CreateDependencies(
            dependencies=[
                DependencyEdgeSpec(
                    from_ticket_id=edge["fromTicketId"], to_ticket_id=edge["toTicketId"]
                )
                for edge in payload.get("dependencies", [])
            ]
        )
    # delete_dependencies (opaque ids) + any unknown op → unchanged projection.
    return None


class ProjectorOperationsLayer:
    """:class:`~specsmither.lifecycle.ports.OperationsLayer` over the pure M0 projector.

    Stateless and DB-free: builds the :data:`MutationOp` for ``op`` / ``payload``,
    runs it through the in-memory projector (which deep-clones, never mutating the
    input), then rebuilds a fresh :class:`SpecFull` from the projected nested model so
    the gate can score the spec as-if-applied.
    """

    def apply_mutation(
        self, op: str, payload: Mapping[str, Any], spec_full: SpecFull
    ) -> SpecFull:
        mutation = _build_mutation_op(op, payload)
        if mutation is None:
            # No faithful in-memory projection (e.g. delete by opaque edge id) →
            # rebuild from the unchanged spec so the return shape stays consistent.
            return _spec_full_from_spec(spec_full.spec)
        projected = project_mutation(spec_full.spec, mutation)
        return _spec_full_from_spec(projected)


# --------------------------------------------------------------------------- #
# The factory (the COUPLED seam, A1 §5)                                         #
# --------------------------------------------------------------------------- #


def make_lifecycle_ports(
    session: Session,
    *,
    clock: Clock | None = None,
    id_generator: IdGenerator | None = None,
) -> LifecyclePorts:
    """Bind the eight :class:`LifecyclePorts` slots over one live ``Session``.

    Every store/adapter shares *session* (one ``BEGIN IMMEDIATE`` per mutation): the
    SQLite planning-session / spec / config stores, the crucible validator, the
    in-memory projector, and ``persist_write_plan`` (the WritePlan executor applied
    in-transaction — the caller owns the surrounding ``Session.begin()`` / commit).
    ``clock`` defaults to UTC-now and ``id_generator`` to the monotonic ULID minter
    (the audit projector orders rows by ``id`` as a chronological cursor).
    """

    def _persist(plan: WritePlan) -> None:
        apply_write_plan(session, plan)

    return LifecyclePorts(
        planning_session_store=SqlitePlanningSessionStore(session),
        spec_store=SqliteSpecStore(session),
        config_store=ConfigStoreSqlite(session),
        validator=CrucibleValidatorAdapter(),
        operations=ProjectorOperationsLayer(),
        persist_write_plan=_persist,
        clock=clock if clock is not None else _utc_now,
        id_generator=id_generator if id_generator is not None else new_ulid,
    )


if TYPE_CHECKING:
    # Structural conformance guards (mypy --strict): each concrete adapter must
    # satisfy its ports Protocol. Never executed.
    def _assert_session_store(
        store: SqlitePlanningSessionStore,
    ) -> _PlanningSessionStoreProto:
        return store

    def _assert_spec_store(store: SqliteSpecStore) -> _SpecStoreProto:
        return store

    def _assert_operations(layer: ProjectorOperationsLayer) -> _OperationsLayerProto:
        return layer
