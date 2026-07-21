"""Pre-check: does the content the write will persist read back cleanly?

The write-boundary safety net. Spec/epic/ticket content is persisted as JSON (or decomposed
into child tables) and read back through the crucible-backed domain records
(``SpecificationRecord`` / ``EpicRecord`` / ``TicketRecord``). If any field is the wrong shape
or carries an off-enum value, the *write* happily persists it (the in-memory projection is
lenient) but the very next *read* raises a pydantic ``ValidationError`` — surfacing as an opaque
``INTERNAL`` error that poisons every subsequent action on the spec.

This guard validates each payload field against the SAME record field type the read uses — after
running the id/order filler (:func:`~specsmither.lifecycle.write_plan.ensure_item_ids` for
spec/epic) and tolerating the id/order the decompose stamps for ticket child arrays — and denies
with the exact field errors, so no un-readable content is ever persisted. Because it drives off
each record's own field annotations it covers EVERY directly-mapped field (goals, scope,
epicTargets, apiContracts, codeReferences, …) with no per-field allow-list to drift, and it can
never be stricter than the read. The ticket child arrays the ``update_ticket`` decompose splits
into child rows are read back through those rows (which accept a bare string as the item text),
not through the raw record annotation, so they are exempt from the raw-shape check here
(``testSpecification.testTypes`` is validated explicitly for its enum). It subsumes the proactive
``field_shape_soft_deny`` / ``enum_field_guards`` (which stay for their friendlier messages).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

from crucible.models.enums import TestType
from pydantic import BaseModel, TypeAdapter, ValidationError

from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord
from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult
from specsmither.lifecycle.write_plan import ensure_item_ids

__all__ = ["content_shape_valid"]

_RECORD_FOR_OP: dict[str, type[BaseModel]] = {
    "update_spec": SpecificationRecord,
    "update_epic": EpicRecord,
    "update_ticket": TicketRecord,
}

#: The ticket child-array fields the ``update_ticket`` decompose splits into child rows.
#: They are persisted and read back through those rows — whose decompose accepts a bare
#: string as the item text — NOT through the raw record annotation, so validating them
#: against the strict record shape would be stricter than both the read AND the writer.
#: Their own decompose + child-row read own the shape. (``testSpecification`` is covered
#: separately by :func:`_ticket_testtypes_blockers`.)
_TICKET_DECOMPOSED_FIELDS: frozenset[str] = frozenset(
    {
        "acceptanceCriteria",
        "implementationSteps",
        "filesToBeCreated",
        "filesToBeModified",
        "filesToBeDeleted",
        "filesToBeReferenced",
        "blueprintReferences",
        "codeSnippets",
        "typeSnippets",
        "testSpecification",
    }
)


@cache
def _field_adapters(op: str) -> dict[str, TypeAdapter[Any]]:
    """A per-op map of {wire-alias | snake-name -> TypeAdapter(field annotation)} for the record."""
    record = _RECORD_FOR_OP[op]
    out: dict[str, TypeAdapter[Any]] = {}
    for name, field in record.model_fields.items():
        adapter: TypeAdapter[Any] = TypeAdapter(field.annotation)
        out[name] = adapter
        if field.alias:
            out[field.alias] = adapter
    return out


def _denial(blockers: list[str]) -> Denied:
    capped = blockers[:25]
    return Denied(
        code="invalid_content",
        message=(
            f"{len(blockers)} content field(s) would not persist/read cleanly. Correct these "
            "and re-send (each item must match its object shape and use valid enum values):\n- "
            + "\n- ".join(capped)
        ),
        context={"field_errors": capped},
        blockers=capped,
    )


def _ticket_testtypes_blockers(fields: Mapping[str, Any]) -> list[str]:
    """The one ticket group the read transforms out of a record field: testSpecification.testTypes."""
    test_spec = fields.get("testSpecification")
    if not isinstance(test_spec, Mapping):
        return []
    test_types = test_spec.get("testTypes")
    if not isinstance(test_types, list):
        return []
    valid = {e.value for e in TestType}
    return [
        f"testSpecification.testTypes[{i}] = '{v}' — use one of: " + ", ".join(sorted(valid))
        for i, v in enumerate(test_types)
        if isinstance(v, str) and v not in valid
    ]


def content_shape_valid(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
) -> PrecheckResult:
    """Deny an update whose content would not read back cleanly through the domain records."""
    if op not in _RECORD_FOR_OP:
        return Accepted()
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        return Accepted()

    # Mint spec/epic array ids first so a missing internal id isn't flagged; ticket child arrays
    # get their id/order stamped by the decompose, so we tolerate id/order errors below instead.
    filled = ensure_item_ids(fields) if op in ("update_spec", "update_epic") else dict(fields)
    adapters = _field_adapters(op)
    blockers: list[str] = []
    for key, value in filled.items():
        if op == "update_ticket" and key in _TICKET_DECOMPOSED_FIELDS:
            continue  # decompose-transformed child array — validated via its child rows, not here
        adapter = adapters.get(key)
        if adapter is None:
            continue  # unknown / decompose-only field — the records allow extras; read ignores it
        try:
            adapter.validate_python(value)
        except ValidationError as exc:
            for err in exc.errors():
                loc = err.get("loc", ())
                if loc and str(loc[-1]) in ("id", "order"):
                    continue  # stamped at the write boundary
                loc_str = ".".join(str(p) for p in loc)
                blockers.append(f"{key}.{loc_str}: {err.get('msg')}" if loc_str else f"{key}: {err.get('msg')}")

    if op == "update_ticket":
        blockers.extend(_ticket_testtypes_blockers(fields))

    return _denial(blockers) if blockers else Accepted()
