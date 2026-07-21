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
    if op == "create_ticket" and current_phase == PlanningPhase.TICKET_EXPANSION:
        # structural_create_in_expansion — create_ticket is native to ticket_decomposition,
        # so in the SCORED ticket_expansion phase it lands as a late op that would roll the
        # session back and orphan a fresh, empty ticket, skewing the gate baseline mid-drive.
        # Soft-deny and point at update_ticket instead. This fires for THIS pair ONLY:
        # create_epic / create_blueprint in an expansion phase are late ops that legitimately
        # roll back and create (Accepted below), and create_ticket is forbidden in
        # epic_expansion, so ticket_expansion is the only scored phase it lands in.
        return Denied(
            code="structural_create_in_expansion",
            message=(
                "ticket_expansion fills in existing tickets — it does not create them. "
                "Creating here would roll the session back to ticket_decomposition and "
                "orphan the new ticket. Use 'update_ticket' on an existing id "
                "(call get_planning_status for the full roster)."
            ),
            context={
                "operation": op,
                "current_phase": current_phase.value,
                "native_phase": PlanningPhase.TICKET_DECOMPOSITION.value,
                "recovery_operation": "update_ticket",
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
