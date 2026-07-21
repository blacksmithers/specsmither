"""Pre-check: does the operation payload match its canonical shape?

The per-operation payload schemas — the deny-gate. Pure: a static table of
pydantic models, one per planning operation, plus the validator that runs a
payload against its model.

Shape rules:

* Synthetic / audit-only ops and any op without a registered schema → accept
  (no validation).
* Payload models **strip** unknown keys (``extra='ignore'``) — except
  ``get_planning_status``, which forbids any key (``extra='forbid'``).
* Non-empty strings use ``min_length=1``; non-empty arrays use ``min_length=1``;
  ``create_dependencies.dependencies`` keeps the ``max_length=5000`` cap.

A malformed payload → :class:`Denied` (``invalid_payload``) carrying the
collected validation errors in ``context``.

The same models back :data:`PLANNING_OPERATION_CONTRACT` (a plain-data view) and
:func:`check_planning_catalog_parity`, so a public tool catalog can be tested to
advertise exactly the shapes this gate enforces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from specsmither.lifecycle.operations_registry import (
    PLANNING_SYNTHETIC_OPERATIONS,
    PlanningOperationName,
)
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = [
    "PLANNING_OPERATION_CONTRACT",
    "CatalogOperationBranch",
    "PlanningOpFieldContract",
    "PlanningOpItemContract",
    "catalog_branches_from_oneof",
    "check_planning_catalog_parity",
    "schema_validate",
]

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


class _JustifyPayload(_Payload):
    scope: _NonEmptyStr
    entityId: _NonEmptyStr
    # reason has no min_length here — the ``justify`` handler enforces the naReason bound
    # (so the deny is the specific ``justification_too_short``, not a generic schema error).
    reason: str


class _UnjustifyPayload(_Payload):
    scope: _NonEmptyStr
    entityId: _NonEmptyStr


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
    "justify": _JustifyPayload,
    "unjustify": _UnjustifyPayload,
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


# --------------------------------------------------------------------------- #
# Canonical operation contract + catalog-parity guard                          #
# --------------------------------------------------------------------------- #
# The deny-gate above is the single source of truth for what a planning payload
# must contain. A public MCP tool catalog (``action_planning_session.oneOf``)
# advertises those same shapes to agents — and if the two drift (the catalog says
# ``epicId`` where the gate wants ``id``, or ``{ticketId,dependsOnId}`` where the
# gate wants ``{fromTicketId,toTicketId}``), an agent that follows the catalog gets
# denied ``invalid_payload``. :data:`PLANNING_OPERATION_CONTRACT` reduces the gate
# models to plain data, and :func:`check_planning_catalog_parity` asserts a catalog
# conforms to it — the coupling test that keeps the two from diverging.


@dataclass(frozen=True)
class PlanningOpItemContract:
    """Required fields of the objects in an operation payload's array field."""

    field: str
    required: tuple[str, ...]


@dataclass(frozen=True)
class PlanningOpFieldContract:
    """A planning operation's canonical payload shape: the required top-level fields,
    plus (optionally) the required fields of a single array-of-objects field."""

    required: tuple[str, ...]
    items: PlanningOpItemContract | None = None


def _required_fields(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(name for name, field in model.model_fields.items() if field.is_required())


def _array_item_model(annotation: Any) -> type[BaseModel] | None:
    """``list[X]`` where ``X`` is a :class:`BaseModel` subclass → ``X``, else ``None``."""
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return args[0]
    return None


def _derive_contract(model: type[BaseModel]) -> PlanningOpFieldContract:
    items: PlanningOpItemContract | None = None
    for name, field in model.model_fields.items():
        element = _array_item_model(field.annotation)
        if element is not None:
            items = PlanningOpItemContract(field=name, required=_required_fields(element))
    return PlanningOpFieldContract(required=_required_fields(model), items=items)


#: Plain-data view of the deny-gate models, ``op → required fields (+ array-item
#: required fields)``. The canonical payload contract every public tool catalog is
#: parity-tested against via :func:`check_planning_catalog_parity`.
PLANNING_OPERATION_CONTRACT: dict[str, PlanningOpFieldContract] = {
    op: _derive_contract(model) for op, model in _SCHEMAS.items()
}


@dataclass(frozen=True)
class CatalogOperationBranch:
    """One ``action_planning_session`` catalog ``oneOf`` branch, reduced to what parity
    needs: the discriminant op name, the payload's required fields, and the required
    fields of the payload's array-of-objects field (if any)."""

    operation: str | None
    payload_required: tuple[str, ...]
    item_field: str | None = None
    item_required: tuple[str, ...] = ()


def catalog_branches_from_oneof(
    one_of: Iterable[Mapping[str, Any]],
) -> list[CatalogOperationBranch]:
    """Reduce a catalog's JSON-schema ``oneOf`` list to :class:`CatalogOperationBranch`.

    Each branch discriminates on ``properties.operation.const`` and carries the per-op
    ``payload`` object schema (its ``required`` list, and the ``required`` of a single
    array-of-objects field). Anything else in the branch is irrelevant to parity.
    """

    branches: list[CatalogOperationBranch] = []
    for branch in one_of:
        props = branch.get("properties", {})
        operation = (props.get("operation") or {}).get("const")
        payload = props.get("payload") or {}
        payload_required = tuple(payload.get("required", ()))
        item_field: str | None = None
        item_required: tuple[str, ...] = ()
        for name, schema in (payload.get("properties") or {}).items():
            if schema.get("type") == "array":
                element = schema.get("items") or {}
                if element.get("type") == "object":
                    item_field = name
                    item_required = tuple(element.get("required", ()))
        branches.append(
            CatalogOperationBranch(operation, payload_required, item_field, item_required)
        )
    return branches


def check_planning_catalog_parity(branches: Sequence[CatalogOperationBranch]) -> list[str]:
    """Assert a tool catalog's ``oneOf`` matches :data:`PLANNING_OPERATION_CONTRACT`.

    Returns a list of human-readable mismatch messages — empty means full parity. Every
    contract op must appear exactly once, with the same required payload fields and (where
    the payload has an array-of-objects field) the same required item fields; a catalog op
    unknown to the deny-gate is also flagged.
    """

    errors: list[str] = []
    by_op: dict[str, list[CatalogOperationBranch]] = {}
    for branch in branches:
        if not branch.operation:
            errors.append(
                f"branch without an operation discriminant: "
                f"required={list(branch.payload_required)}"
            )
            continue
        by_op.setdefault(branch.operation, []).append(branch)

    for op, contract in PLANNING_OPERATION_CONTRACT.items():
        found = by_op.get(op)
        if not found:
            errors.append(f"{op}: missing from catalog oneOf")
            continue
        if len(found) > 1:
            errors.append(f"{op}: {len(found)} branches (expected 1 — collapse variants)")
            continue
        branch = found[0]
        if set(branch.payload_required) != set(contract.required):
            errors.append(
                f"{op}: payload required={sorted(branch.payload_required)} "
                f"but lifecycle contract={sorted(contract.required)}"
            )
        if contract.items is not None:
            if branch.item_field != contract.items.field:
                errors.append(
                    f"{op}: array-item field={branch.item_field!r} "
                    f"but contract={contract.items.field!r}"
                )
            elif set(branch.item_required) != set(contract.items.required):
                errors.append(
                    f"{op}.{contract.items.field}[]: item required="
                    f"{sorted(branch.item_required)} but contract="
                    f"{sorted(contract.items.required)}"
                )

    for op in by_op:
        if op not in PLANNING_OPERATION_CONTRACT:
            errors.append(f"catalog declares op '{op}' with no lifecycle schema (unknown to the deny-gate)")

    return errors
