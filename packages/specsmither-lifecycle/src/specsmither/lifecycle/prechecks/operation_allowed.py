"""Pre-check: is ``op`` allowed in the session's current phase?

Pure. Delegates the phase decision to
:func:`~specsmither.lifecycle.operations_registry.classify_operation_call`:

* ``forbidden`` → :class:`Denied` (``current_phase_must_finish_first``).
* ``native`` → :class:`Accepted` (``rollback=False``).
* ``late`` → :class:`Accepted` with ``rollback=True`` — the caller is working
  past the op's native phase, so the APS pipeline rewinds the session — **except**
  a late ``update_*`` whose payload touches ONLY ``fieldDeclarations``: that is an
  N/A justification, not a structural body change, and is exempt from rollback
  (``rollback=False``). The exemption lets an agent declare e.g. ``dependencies``
  N/A during ``cross_validation`` without being thrown back to ``ticket_expansion``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.operations_registry import (
    PlanningOperationName,
    classify_operation_call,
    get_operation_def,
)
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["operation_allowed"]

_UPDATE_OPS: frozenset[str] = frozenset({"update_spec", "update_epic", "update_ticket"})


def operation_allowed(
    op: PlanningOperationName,
    current_phase: PlanningPhase,
    payload: Mapping[str, Any] | None = None,
) -> PrecheckResult:
    """Classify ``op`` in ``current_phase`` → :class:`Accepted` | :class:`Denied`.

    ``payload`` is consulted only for the ``fieldDeclarations``-only rollback
    exemption on a late ``update_*``.
    """

    classification = classify_operation_call(op, current_phase)
    if classification == "forbidden":
        defn = get_operation_def(op)
        native = defn.native_phase if defn is not None else None
        native_value = native.value if isinstance(native, PlanningPhase) else native
        return Denied(
            code="current_phase_must_finish_first",
            message=(
                f"Operation '{op}' is not allowed in phase '{current_phase.value}'. "
                "Complete this phase first."
            ),
            context={
                "operation": op,
                "current_phase": current_phase.value,
                "native_phase": native_value,
            },
        )

    rollback = classification == "late" and not _is_field_declarations_only_update(op, payload)
    if (
        rollback
        and op.startswith("create_")
        and current_phase in (PlanningPhase.EPIC_EXPANSION, PlanningPhase.TICKET_EXPANSION)
    ):
        # structural_create_in_expansion (2bf6d24f) — a structural create in a SCORED
        # expansion phase must NOT silently roll the session back to its native phase
        # (which orphans the freshly-created entity and skews the gate baseline mid-drive).
        # Soft-deny and name the recovery update_ op instead.
        recovery = op.replace("create_", "update_", 1)
        return Denied(
            code="structural_create_in_expansion",
            message=(
                f"Cannot '{op}' during the scored '{current_phase.value}' phase — it would "
                f"orphan the new entity and skew the gate. Refine existing entities with "
                f"'{recovery}', or complete this phase first."
            ),
            context={
                "operation": op,
                "current_phase": current_phase.value,
                "recovery_operation": recovery,
            },
        )
    return Accepted(rollback=rollback)


def _is_field_declarations_only_update(
    op: PlanningOperationName, payload: Mapping[str, Any] | None
) -> bool:
    """``True`` iff ``op`` is an ``update_*`` whose ``fields`` are only ``fieldDeclarations``.

    A non-empty ``fields`` map whose every key is ``fieldDeclarations``.
    """

    if op not in _UPDATE_OPS:
        return False
    if not isinstance(payload, Mapping):
        return False
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        return False
    keys = list(fields.keys())
    return len(keys) > 0 and all(k == "fieldDeclarations" for k in keys)
