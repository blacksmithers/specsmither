"""Pre-check: enum-poison write guard on the content arrays.

Pure. An agent may author a plausible-but-off-enum value inside a content array
(``nonFunctionalRequirements[].category = 'portability'``, ``guardrails[].category =
'architecture'``, ``techStack[].layer = 'language'``, ``goals[].type = 'foo'``, …). The
write boundary constructs the typed record over the crucible sub-models, whose enums then
RAISE a pydantic ``ValidationError`` — surfacing as an opaque ``INTERNAL`` error the agent
cannot recover from. This guard denies the value up front (``invalid_enum_value``) and
LISTS the valid options, so the agent self-corrects.

The vocabularies are read straight off the crucible sub-models (via the SpecSmither domain
records) so this can never drift from the read schema.
"""

from __future__ import annotations

import enum
import typing
from collections.abc import Mapping
from typing import Any

from crucible.models.planning import (
    Goal,
    Guardrail,
    NonFunctionalRequirement,
    Requirement,
    TechStackItem,
)
from pydantic import BaseModel

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["ENUM_ARRAY_GUARDS", "enum_field_guards"]


def _enum_values(model: type[BaseModel], field: str) -> tuple[str, ...]:
    """Extract the valid enum ``.value`` strings for ``model``'s ``field`` (Optional-safe)."""
    annotation = model.model_fields[field].annotation

    def members(tp: Any) -> tuple[str, ...] | None:
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            return tuple(str(e.value) for e in tp)
        return None

    direct = members(annotation)
    if direct is not None:
        return direct
    for arg in typing.get_args(annotation):
        found = members(arg)
        if found is not None:
            return found
    return ()


#: wire array field -> (item enum sub-field, valid values). ``apiContracts[].type`` is
#: guarded separately by ``field_shape_soft_deny`` (it also case-normalises at the write).
ENUM_ARRAY_GUARDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "goals": ("type", _enum_values(Goal, "type")),
    "requirements": ("type", _enum_values(Requirement, "type")),
    "nonFunctionalRequirements": ("category", _enum_values(NonFunctionalRequirement, "category")),
    "guardrails": ("category", _enum_values(Guardrail, "category")),
    "techStack": ("layer", _enum_values(TechStackItem, "layer")),
}


def enum_field_guards(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
) -> PrecheckResult:
    """Deny an update whose content-array item carries an off-enum value."""
    if op not in ("update_spec", "update_epic"):
        return Accepted()
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        return Accepted()

    # Collect EVERY off-enum value across all guarded fields so the agent can fix them all
    # in one retry instead of one round-trip per offender.
    violations: list[dict[str, Any]] = []
    blockers: list[str] = []
    for wire_field, (sub_field, valid) in ENUM_ARRAY_GUARDS.items():
        if not valid:
            continue
        items = fields.get(wire_field)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            value = item.get(sub_field)
            if isinstance(value, str) and value not in valid:
                violations.append(
                    {"field": f"{wire_field}[{index}].{sub_field}", "value": value, "valid_values": list(valid)}
                )
                blockers.append(
                    f"{wire_field}[{index}].{sub_field} = '{value}' — use one of: {', '.join(valid)}"
                )

    if violations:
        return Denied(
            code="invalid_enum_value",
            message=(
                f"{len(violations)} field(s) carry an off-enum value. Correct them to a listed "
                "value and re-send the operation:\n- " + "\n- ".join(blockers)
            ),
            context={"violations": violations},
            blockers=blockers,
        )
    return Accepted()
