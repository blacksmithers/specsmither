"""Pure in-memory ``OperationsProjector`` (work item #17).

Port of ``packages/core/src/factory/apply-mutation.ts`` (the production
``IOperationsLayer``). The planning lifecycle calls :func:`apply_mutation` to
project the *would-be* post-mutation state of a spec **in memory, without
persisting**, so the validator/gate can score the spec AS IF the operation had
already been applied. The real write happens later, via the executor adapter
(#16), against the stores.

Faithful-port notes (the seam differs from SpecForge in one structural way):

* SpecForge's projector clones a ``SpecFull`` wrapper — a nested
  ``Specification`` plus three *flat* side-projections (``epics`` / ``blueprints``
  / ``dependencies``) the lifecycle pre-checks read. SpecSmither's gate is the
  crucible validator, which scores the **nested** :class:`crucible.models.Specification`
  *directly* (it is the single source of truth — exactly what
  :func:`specsmither.domain.records.build_spec_full` recomposes). So here the
  "flat epics list" and "flat blueprints" the task names are simply
  ``spec.epics`` / ``spec.blueprints``, and "flat dependencies on tickets" are the
  nested ``ticket.dependencies`` :class:`~crucible.models.DependencyLink`\\ s. The
  projector therefore mutates the nested tree in place and keeps it internally
  consistent by construction (created epics carry ``specification_id``; created
  tickets carry ``epic_id``; dependency links land on the dependent ticket).
* :func:`project_dependency_edges` is provided as the explicit flat-dependency
  derivation (the SpecSmither analogue of ``projectFlat``'s ``dependencies``): it
  is the exact inverse of ``build_spec_full``'s edge→link mapping, so a projected
  tree's edges line up byte-for-byte with what the persistence path would store.

Purity contract: :meth:`OperationsProjector.apply` deep-clones its input
(``model_copy(deep=True)``), mutates only the clone, and returns it — the caller's
spec is never touched. No SQLAlchemy / DB / store access whatsoever; this is a
pure function over crucible models. Projection-synthesized ids are deterministic
(``__proj_*``, mirroring the TS) so the function stays referentially transparent;
callers may pass an explicit ``id`` to override.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

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
# This is a representative, faithful subset of the apply-mutation cases; the full
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

    Unlike the SpecForge projector (which left state unchanged because its nested
    ``DependencyLink`` has no edge id), SpecSmither edges are identified by the
    ``(dependent, depended-on)`` pair — the persisted ``UNIQUE(ticket_id,
    depends_on_id)`` key — so the removal projects faithfully here.
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
    depends_on_id=Y)``. (No direction flip by ``type`` — that is a SpecForge
    lifecycle-graph concern; SpecSmither's persisted edge keeps the dependent as
    ``ticket_id`` regardless of ``requires``/``blocks``.)
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
