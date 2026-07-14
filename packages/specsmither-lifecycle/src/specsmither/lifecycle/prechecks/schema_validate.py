"""Pre-check: does the operation payload match its canonical shape?

Faithful port of ``planning/pre-checks/schema-zod.ts`` (A1 §1.4) with the
per-operation Zod schemas re-expressed as pydantic models. Pure.

Parity rules carried over from the Zod source:

* Synthetic / audit-only ops and any op without a registered schema → accept
  (no validation).
* Object schemas **strip** unknown keys by default in Zod, so the pydantic
  models use ``extra='ignore'`` — except ``get_planning_status`` (Zod ``.strict()``),
  which uses ``extra='forbid'`` (any key is a denial).
* ``z.string().min(1)`` → ``min_length=1``; array ``.min(1)`` → ``min_length=1``;
  ``create_dependencies.dependencies`` keeps the ``max(5000)`` cap.

A malformed payload → :class:`Denied` (``invalid_payload``) carrying the
collected validation errors in ``context``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from specsmither.lifecycle.operations_registry import (
    PLANNING_SYNTHETIC_OPERATIONS,
    PlanningOperationName,
)
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["schema_validate"]

#: A non-empty string (Zod ``z.string().min(1)``).
_NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class _Payload(BaseModel):
    """Base for the payload models — unknown keys stripped (Zod default)."""

    model_config = ConfigDict(extra="ignore")


class _UpdateSpecPayload(_Payload):
    fields: dict[str, Any]


class _CreateEpicPayload(_Payload):
    title: _NonEmptyStr
    description: str | None = None


class _UpdateEpicPayload(_Payload):
    id: _NonEmptyStr
    fields: dict[str, Any]


class _DeleteEpicPayload(_Payload):
    id: _NonEmptyStr
    cascadeRemoveDependencies: bool | None = None


class _CreateTicketPayload(_Payload):
    epicId: _NonEmptyStr
    title: _NonEmptyStr
    description: str | None = None
    # ticketType is CREATE-only (00c468fe): honoured here (default implementation) but
    # NOT writable on update_ticket, so a type-flip can't rebalance the impl:verification
    # ratio past an already-passed ticket_decomposition gate.
    ticketType: Literal["implementation", "verification"] | None = None


class _UpdateTicketPayload(_Payload):
    id: _NonEmptyStr
    fields: dict[str, Any]


class _DeleteTicketPayload(_Payload):
    id: _NonEmptyStr
    cascadeRemoveDependencies: bool | None = None


class _CreateBlueprintPayload(_Payload):
    title: _NonEmptyStr
    category: _NonEmptyStr


class _UpdateBlueprintPayload(_Payload):
    id: _NonEmptyStr
    fields: dict[str, Any]


class _DeleteBlueprintPayload(_Payload):
    id: _NonEmptyStr


class _LinkBlueprintToTicketsPayload(_Payload):
    blueprintId: _NonEmptyStr
    ticketIds: Annotated[list[_NonEmptyStr], Field(min_length=1)]


class _UnlinkBlueprintToTicketsPayload(_Payload):
    blueprintId: _NonEmptyStr
    ticketIds: Annotated[list[_NonEmptyStr], Field(min_length=1)]


class _DependencyEdgePayload(_Payload):
    fromTicketId: str
    toTicketId: str


class _CreateDependenciesPayload(_Payload):
    dependencies: Annotated[list[_DependencyEdgePayload], Field(max_length=5000)]


class _DeleteDependenciesPayload(_Payload):
    dependencyIds: Annotated[list[_NonEmptyStr], Field(min_length=1)]


class _GetPlanningStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


#: Per-operation payload models (the deny-gate). Ops absent here are accepted.
_SCHEMAS: dict[str, type[BaseModel]] = {
    "update_spec": _UpdateSpecPayload,
    "create_epic": _CreateEpicPayload,
    "update_epic": _UpdateEpicPayload,
    "delete_epic": _DeleteEpicPayload,
    "create_ticket": _CreateTicketPayload,
    "update_ticket": _UpdateTicketPayload,
    "delete_ticket": _DeleteTicketPayload,
    "create_blueprint": _CreateBlueprintPayload,
    "update_blueprint": _UpdateBlueprintPayload,
    "delete_blueprint": _DeleteBlueprintPayload,
    "link_blueprint_to_tickets": _LinkBlueprintToTicketsPayload,
    "unlink_blueprint_to_tickets": _UnlinkBlueprintToTicketsPayload,
    "create_dependencies": _CreateDependenciesPayload,
    "delete_dependencies": _DeleteDependenciesPayload,
    "get_planning_status": _GetPlanningStatusPayload,
}

_SYNTHETIC: frozenset[str] = frozenset(PLANNING_SYNTHETIC_OPERATIONS)


def schema_validate(op: PlanningOperationName, payload: Any) -> PrecheckResult:
    """Validate ``payload`` against ``op``'s schema → :class:`Accepted` | :class:`Denied`."""

    if op in _SYNTHETIC:
        return Accepted()

    model = _SCHEMAS.get(op)
    if model is None:
        return Accepted()

    try:
        model.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        blockers = [f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in errors]
        return Denied(
            code="invalid_payload",
            message=(
                f"Payload for operation '{op}' is malformed ({len(blockers)} problem(s)). "
                "Correct these and re-send:\n- " + "\n- ".join(blockers)
            ),
            context={"errors": errors},
            blockers=blockers,
        )
    return Accepted()
