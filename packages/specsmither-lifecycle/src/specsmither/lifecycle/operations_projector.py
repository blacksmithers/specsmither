"""Pure in-memory ``OperationsProjector``.

The planning lifecycle calls :func:`apply_mutation` to
project the *would-be* post-mutation state of a spec **in memory, without
persisting**, so the validator/gate can score the spec AS IF the operation had
already been applied. The real write happens later, via the executor adapter,
against the stores.

Design notes (one structural characteristic of the projector seam):

* A projector could clone a ``SpecFull`` wrapper — a nested
  ``Specification`` plus three *flat* side-projections (``epics`` / ``blueprints``
  / ``dependencies``) the lifecycle pre-checks read. SpecSmither's gate is the
  crucible validator, which scores the **nested** :class:`crucible.models.Specification`
  *directly* (it is the single source of truth — exactly what
  :func:`specsmither.domain.records.build_spec_full` recomposes). So here the
  "flat epics list" and "flat blueprints" are simply
  ``spec.epics`` / ``spec.blueprints``, and "flat dependencies on tickets" are the
  nested ``ticket.dependencies`` :class:`~crucible.models.DependencyLink`\\ s. The
  projector therefore mutates the nested tree in place and keeps it internally
  consistent by construction (created epics carry ``specification_id``; created
  tickets carry ``epic_id``; dependency links land on the dependent ticket).
* :func:`project_dependency_edges` is provided as the explicit flat-dependency
  derivation: it is the exact inverse of ``build_spec_full``'s edge→link mapping,
  so a projected tree's edges line up exactly with what the persistence path
  would store.

Purity contract: :meth:`OperationsProjector.apply` deep-clones its input
(``model_copy(deep=True)``), mutates only the clone, and returns it — the caller's
spec is never touched. No SQLAlchemy / DB / store access whatsoever; this is a
pure function over crucible models. Projection-synthesized ids are deterministic
(``__proj_*``) so the function stays referentially transparent;
callers may pass an explicit ``id`` to override.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from crucible.models import (
    Blueprint,
    BlueprintReference,
    DependencyLink,
    Epic,
    Specification,
    Ticket,
)
from crucible.models.enums import (
    BlueprintCategory,
    Complexity,
    DependencyType,
    TicketType,
)
from pydantic import BaseModel

from specsmither.domain.records import DependencyEdge
from specsmither.lifecycle.ports import (
    BlueprintRef,
    EpicFull,
    SpecDependencyEdge,
    SpecFull,
    TicketRef,
)
from specsmither.lifecycle.write_plan_types import to_snake

__all__ = [
    "CreateBlueprint",
    "CreateDependencies",
    "CreateEpic",
    "CreateTicket",
    "DeleteBlueprint",
    "DeleteDependencies",
    "DeleteEpic",
    "DeleteTicket",
    "DependencyEdgeSpec",
    "LinkBlueprintToTickets",
    "MutationOp",
    "OperationsProjector",
    "ProjectorOperationsLayer",
    "UnlinkBlueprintFromTickets",
    "UpdateBlueprint",
    "UpdateEpic",
    "UpdateSpec",
    "UpdateTicket",
    "apply_mutation",
    "project_dependency_edges",
]


# --------------------------------------------------------------------------- #
# Operation tagged union (kind + payload)                                      #
# --------------------------------------------------------------------------- #
#
# One frozen dataclass per planning mutation kind, discriminated by ``kind``.
# This is a representative subset of the apply-mutation cases; the full
# 15-op planning registry (0.1.0) is the consumer that dispatches these. Update
# ops carry a ``fields`` map keyed by **snake_case** model attribute names (the
# Pythonic field names, not the camelCase wire aliases).


@dataclass(frozen=True)
class UpdateSpec:
    """Patch top-level spec fields (``update_spec``)."""

    fields: Mapping[str, Any]
    kind: Literal["update_spec"] = "update_spec"


@dataclass(frozen=True)
class CreateEpic:
    """Append a new epic (``create_epic``)."""

    title: str
    description: str = ""
    objective: str = ""
    id: str | None = None
    kind: Literal["create_epic"] = "create_epic"


@dataclass(frozen=True)
class UpdateEpic:
    """Patch fields on the epic ``id`` (``update_epic``)."""

    id: str
    fields: Mapping[str, Any]
    kind: Literal["update_epic"] = "update_epic"


@dataclass(frozen=True)
class DeleteEpic:
    """Remove the epic ``id`` and its tickets (``delete_epic``)."""

    id: str
    kind: Literal["delete_epic"] = "delete_epic"


@dataclass(frozen=True)
class CreateTicket:
    """Append a new ticket under ``epic_id`` (``create_ticket``)."""

    epic_id: str
    title: str
    description: str = ""
    ticket_type: TicketType = TicketType.IMPLEMENTATION
    complexity: Complexity = Complexity.MEDIUM
    id: str | None = None
    kind: Literal["create_ticket"] = "create_ticket"


@dataclass(frozen=True)
class UpdateTicket:
    """Patch fields on the ticket ``id`` (``update_ticket``)."""

    id: str
    fields: Mapping[str, Any]
    kind: Literal["update_ticket"] = "update_ticket"


@dataclass(frozen=True)
class DeleteTicket:
    """Remove the ticket ``id`` from whichever epic holds it (``delete_ticket``)."""

    id: str
    kind: Literal["delete_ticket"] = "delete_ticket"


@dataclass(frozen=True)
class CreateBlueprint:
    """Append a new blueprint (``create_blueprint``)."""

    title: str
    category: BlueprintCategory
    content: str = ""
    id: str | None = None
    kind: Literal["create_blueprint"] = "create_blueprint"


@dataclass(frozen=True)
class UpdateBlueprint:
    """Patch fields on the blueprint ``id`` (``update_blueprint``)."""

    id: str
    fields: Mapping[str, Any]
    kind: Literal["update_blueprint"] = "update_blueprint"


@dataclass(frozen=True)
class DeleteBlueprint:
    """Remove the blueprint ``id`` (``delete_blueprint``)."""

    id: str
    kind: Literal["delete_blueprint"] = "delete_blueprint"


@dataclass(frozen=True)
class LinkBlueprintToTickets:
    """Attach ``blueprint_id`` to each listed ticket (``link_blueprint_to_tickets``)."""

    blueprint_id: str
    ticket_ids: Sequence[str]
    kind: Literal["link_blueprint_to_tickets"] = "link_blueprint_to_tickets"


@dataclass(frozen=True)
class UnlinkBlueprintFromTickets:
    """Detach ``blueprint_id`` from each listed ticket (``unlink_blueprint_to_tickets``)."""

    blueprint_id: str
    ticket_ids: Sequence[str]
    kind: Literal["unlink_blueprint_to_tickets"] = "unlink_blueprint_to_tickets"


@dataclass(frozen=True)
class DependencyEdgeSpec:
    """A directed edge: ``from_ticket_id`` depends on ``to_ticket_id``.

    Mirrors :class:`~specsmither.domain.records.DependencyEdge`
    (``ticket_id``→``depends_on_id``): the *from* ticket is the dependent one that
    receives the :class:`~crucible.models.DependencyLink`; the *to* ticket is the
    depended-on target the link points at.
    """

    from_ticket_id: str
    to_ticket_id: str
    type: DependencyType = DependencyType.REQUIRES


@dataclass(frozen=True)
class CreateDependencies:
    """Add one or more dependency edges (``create_dependencies``)."""

    dependencies: Sequence[DependencyEdgeSpec] = ()
    kind: Literal["create_dependencies"] = "create_dependencies"


@dataclass(frozen=True)
class DeleteDependencies:
    """Remove one or more dependency edges by ``(from, to)`` pair (``delete_dependencies``).

    SpecSmither edges are identified by the ``(dependent, depended-on)`` pair —
    the persisted ``UNIQUE(ticket_id, depends_on_id)`` key — so the removal
    projects cleanly in memory.
    """

    dependencies: Sequence[DependencyEdgeSpec] = ()
    kind: Literal["delete_dependencies"] = "delete_dependencies"


MutationOp = (
    UpdateSpec
    | CreateEpic
    | UpdateEpic
    | DeleteEpic
    | CreateTicket
    | UpdateTicket
    | DeleteTicket
    | CreateBlueprint
    | UpdateBlueprint
    | DeleteBlueprint
    | LinkBlueprintToTickets
    | UnlinkBlueprintFromTickets
    | CreateDependencies
    | DeleteDependencies
)


# --------------------------------------------------------------------------- #
# Pure tree helpers                                                            #
# --------------------------------------------------------------------------- #


def _assign(model: BaseModel, fields: Mapping[str, Any]) -> None:
    """Patch a model's attributes in place (snake_case keys; ``extra="allow"``)."""
    for key, value in fields.items():
        setattr(model, key, value)


def _find_epic(spec: Specification, epic_id: str) -> Epic | None:
    return next((epic for epic in spec.epics if epic.id == epic_id), None)


def _find_ticket(spec: Specification, ticket_id: str) -> Ticket | None:
    for epic in spec.epics:
        found = next((t for t in epic.tickets if t.id == ticket_id), None)
        if found is not None:
            return found
    return None


def _link_blueprint(
    spec: Specification, blueprint_id: str, ticket_ids: set[str], *, link: bool
) -> None:
    """Add/remove a :class:`BlueprintReference` to/from each named ticket."""
    for epic in spec.epics:
        for ticket in epic.tickets:
            if ticket.id not in ticket_ids:
                continue
            if link:
                if not any(ref.blueprint_id == blueprint_id for ref in ticket.blueprint_references):
                    ticket.blueprint_references.append(
                        BlueprintReference(blueprint_id=blueprint_id)
                    )
            else:
                ticket.blueprint_references = [
                    ref for ref in ticket.blueprint_references if ref.blueprint_id != blueprint_id
                ]


def _add_dependency(spec: Specification, edge: DependencyEdgeSpec) -> None:
    ticket = _find_ticket(spec, edge.from_ticket_id)
    if ticket is None:
        return
    if any(link.ticket_id == edge.to_ticket_id for link in ticket.dependencies):
        return
    ticket.dependencies.append(DependencyLink(ticket_id=edge.to_ticket_id, type=edge.type))


def _remove_dependency(spec: Specification, edge: DependencyEdgeSpec) -> None:
    ticket = _find_ticket(spec, edge.from_ticket_id)
    if ticket is None:
        return
    # Identity is the (from, to) pair — the persisted UNIQUE key — independent of type.
    ticket.dependencies = [
        link for link in ticket.dependencies if link.ticket_id != edge.to_ticket_id
    ]


def project_dependency_edges(spec: Specification) -> list[DependencyEdge]:
    """Flatten the nested dependency links back into directed edge records.

    The exact inverse of :func:`specsmither.domain.records.build_spec_full`'s
    edge→link mapping: a :class:`~crucible.models.DependencyLink` with
    ``ticket_id == Y`` on ticket ``X`` becomes ``DependencyEdge(ticket_id=X,
    depends_on_id=Y)``. (No direction flip by ``type``; SpecSmither's persisted
    edge keeps the dependent as ``ticket_id`` regardless of ``requires``/``blocks``.)
    """
    edges: list[DependencyEdge] = []
    for epic in spec.epics:
        for ticket in epic.tickets:
            for link in ticket.dependencies:
                edges.append(
                    DependencyEdge(
                        ticket_id=ticket.id, depends_on_id=link.ticket_id, type=link.type
                    )
                )
    return edges


# --------------------------------------------------------------------------- #
# Projector                                                                    #
# --------------------------------------------------------------------------- #


class OperationsProjector:
    """Pure projector: ``apply(spec, op)`` → post-mutation spec (no persistence)."""

    def apply(self, spec: Specification, op: MutationOp) -> Specification:
        """Deep-clone ``spec``, apply ``op`` to the clone, and return it.

        The input ``spec`` is never mutated (deep clone). The returned
        :class:`~crucible.models.Specification` is the gate's scoring input,
        internally consistent by construction.
        """
        projected = spec.model_copy(deep=True)

        match op:
            case UpdateSpec(fields=fields):
                _assign(projected, fields)

            case CreateEpic():
                projected.epics.append(
                    Epic(
                        id=op.id or f"__proj_epic_{len(projected.epics) + 1}",
                        specification_id=projected.id,
                        title=op.title,
                        description=op.description,
                        objective=op.objective,
                    )
                )

            case UpdateEpic(id=epic_id, fields=fields):
                epic = _find_epic(projected, epic_id)
                if epic is not None:
                    _assign(epic, fields)

            case DeleteEpic(id=epic_id):
                projected.epics = [e for e in projected.epics if e.id != epic_id]

            case CreateTicket():
                epic = _find_epic(projected, op.epic_id)
                if epic is not None:
                    epic.tickets.append(
                        Ticket(
                            id=op.id or f"__proj_ticket_{len(epic.tickets) + 1}",
                            epic_id=epic.id,
                            title=op.title,
                            description=op.description,
                            ticket_type=op.ticket_type,
                            complexity=op.complexity,
                        )
                    )

            case UpdateTicket(id=ticket_id, fields=fields):
                ticket = _find_ticket(projected, ticket_id)
                if ticket is not None:
                    _assign(ticket, fields)

            case DeleteTicket(id=ticket_id):
                for epic in projected.epics:
                    epic.tickets = [t for t in epic.tickets if t.id != ticket_id]

            case CreateBlueprint():
                projected.blueprints.append(
                    Blueprint(
                        id=op.id or f"__proj_bp_{len(projected.blueprints) + 1}",
                        title=op.title,
                        category=op.category,
                        content=op.content,
                    )
                )

            case UpdateBlueprint(id=blueprint_id, fields=fields):
                blueprint = next(
                    (b for b in projected.blueprints if b.id == blueprint_id), None
                )
                if blueprint is not None:
                    _assign(blueprint, fields)

            case DeleteBlueprint(id=blueprint_id):
                projected.blueprints = [b for b in projected.blueprints if b.id != blueprint_id]

            case LinkBlueprintToTickets(blueprint_id=blueprint_id, ticket_ids=ticket_ids):
                _link_blueprint(projected, blueprint_id, set(ticket_ids), link=True)

            case UnlinkBlueprintFromTickets(blueprint_id=blueprint_id, ticket_ids=ticket_ids):
                _link_blueprint(projected, blueprint_id, set(ticket_ids), link=False)

            case CreateDependencies(dependencies=edges):
                for edge in edges:
                    _add_dependency(projected, edge)

            case DeleteDependencies(dependencies=edges):
                for edge in edges:
                    _remove_dependency(projected, edge)

        return projected


_DEFAULT_PROJECTOR = OperationsProjector()


def apply_mutation(spec: Specification, op: MutationOp) -> Specification:
    """Module-level convenience over :meth:`OperationsProjector.apply`."""
    return _DEFAULT_PROJECTOR.apply(spec, op)


# --------------------------------------------------------------------------- #
# OperationsLayer — the pure lifecycle ports.OperationsLayer implementation     #
# --------------------------------------------------------------------------- #
#
# Bridges the lifecycle's camelCase wire ops to the :data:`MutationOp` union,
# applies them through the pure projector above (no DB), and re-derives a fresh
# :class:`~specsmither.lifecycle.ports.SpecFull` from the projected nested model
# so the gate can score the spec as-if-applied. DB-free: this is the pure
# OperationsLayer the coupled seam (``adapters/lifecycle_ports.py``) binds.


def _enum_str(value: Any) -> str:
    """Coerce an enum member to its plain ``.value`` string (passthrough for ``str``)."""
    return value.value if isinstance(value, Enum) else str(value)


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


def _snake_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of an update ``fields`` map with camelCase wire keys snake-cased.

    The :data:`MutationOp` update dataclasses key ``fields`` by the snake_case crucible
    attribute name (``setattr`` lands on the real field); the lifecycle wire payload
    is camelCase, so the keys are normalised (``to_snake`` is idempotent on snake input).
    Values pass through untouched (re-validated when crucible re-parses the projected spec).
    """
    return {to_snake(str(key)): value for key, value in raw.items()}


def _build_mutation_op(op: str, payload: Mapping[str, Any]) -> MutationOp | None:
    """Map a lifecycle op name + camelCase payload to its :data:`MutationOp`, or ``None``.

    ``None`` means "no in-memory projection": ``delete_dependencies`` carries
    opaque edge ids (``dependencyIds``) the nested tree cannot resolve to ``(from, to)``
    pairs — the projection is a no-op. An unknown op name is likewise a no-op.
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
    """:class:`~specsmither.lifecycle.ports.OperationsLayer` over the pure projector.

    Stateless and DB-free: builds the :data:`MutationOp` for ``op`` / ``payload``,
    runs it through the in-memory projector (which deep-clones, never mutating the
    input), then rebuilds a fresh :class:`SpecFull` from the projected nested model so
    the gate can score the spec as-if-applied.
    """

    def apply_mutation(self, op: str, payload: Mapping[str, Any], spec_full: SpecFull) -> SpecFull:
        mutation = _build_mutation_op(op, payload)
        if mutation is None:
            # No in-memory projection (e.g. delete by opaque edge id) →
            # rebuild from the unchanged spec so the return shape stays consistent.
            return _spec_full_from_spec(spec_full.spec)
        projected = apply_mutation(spec_full.spec, mutation)
        return _spec_full_from_spec(projected)


if TYPE_CHECKING:
    from specsmither.lifecycle.ports import OperationsLayer as _OperationsLayerProto

    def _assert_operations(layer: ProjectorOperationsLayer) -> _OperationsLayerProto:
        # Structural conformance guard (mypy --strict): never executed.
        return layer
