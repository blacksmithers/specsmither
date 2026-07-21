"""Pre-check: would a create/delete breach the entity-count bounds?

Pure. The **validator** config (domain ``planning``) is the single owner of the
entity-count bounds. This lifecycle hard-deny reads the ``.default`` leaf of each
``ThresholdEntry`` off the resolved
``ValidatorConfig.structuralRequirements.{arrayMinCounts,arrayMaxCounts}``:

* ``...arrayMaxCounts.specification.epics.default`` — max epics per spec.
* ``...arrayMinCounts.specification.epics.default`` — min epics per spec.
* ``...arrayMaxCounts.epic.tickets.default`` — max tickets per epic.
* ``...arrayMinCounts.epic.tickets.default`` — min tickets per epic.

(``structuralRequirements.arrayCountPhases`` is phase-*scoping*, NOT count
values — never read here.)

**Boundary alignment** with the validator advisory (which checks the POST count,
``epicsCount > max``): this pre-check checks the PRE-create count
(``len(epics) >= max``). Both trip at the same real count — keep the ``>=`` / ``<=``.
A missing bound (``None``) disables that side of the check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["count_bounds"]


def _count_default(
    config: Mapping[str, Any], counts_key: str, parent: str, child: str
) -> int | float | None:
    """Read ``structuralRequirements.<counts_key>.<parent>.<child>.default`` safely.

    Returns ``None`` (disables the bound) when any level is absent or the leaf is
    not a number — a safe walk down ``specification`` → ``epics`` → ``default``.
    """

    structural = config.get("structuralRequirements")
    if not isinstance(structural, Mapping):
        return None
    counts = structural.get(counts_key)
    parent_map = counts.get(parent) if isinstance(counts, Mapping) else None
    child_map = parent_map.get(child) if isinstance(parent_map, Mapping) else None
    value = child_map.get("default") if isinstance(child_map, Mapping) else None
    return value if isinstance(value, int | float) else None


def count_bounds(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
    config: Mapping[str, Any],
) -> PrecheckResult:
    """Enforce min/max epic & ticket-per-epic bounds for create/delete ops."""

    max_epics = _count_default(config, "arrayMaxCounts", "specification", "epics")
    min_epics = _count_default(config, "arrayMinCounts", "specification", "epics")
    max_tickets = _count_default(config, "arrayMaxCounts", "epic", "tickets")
    min_tickets = _count_default(config, "arrayMinCounts", "epic", "tickets")

    if op == "create_epic":
        current = len(spec_full.epics)
        if max_epics is not None and current >= max_epics:
            return Denied(
                code="max_epics_exceeded",
                message=(
                    f"Cannot create another epic: the spec already has {current} "
                    f"epics (max {max_epics})."
                ),
                context={"current": current, "max": max_epics},
            )
        return Accepted()

    if op == "create_ticket":
        epic = next((e for e in spec_full.epics if e.id == payload.get("epicId")), None)
        if epic is not None and max_tickets is not None and len(epic.tickets) >= max_tickets:
            return Denied(
                code="max_tickets_per_epic_exceeded",
                message=(
                    f"Cannot create another ticket in epic '{epic.id}': it already "
                    f"has {len(epic.tickets)} tickets (max {max_tickets})."
                ),
                context={"epic_id": epic.id, "current": len(epic.tickets), "max": max_tickets},
            )
        return Accepted()

    if op == "delete_epic":
        current = len(spec_full.epics)
        if min_epics is not None and current <= min_epics:
            return Denied(
                code="min_epics_not_met",
                message=(
                    f"Cannot delete this epic: the spec must keep at least {min_epics} "
                    f"epic(s) (currently {current})."
                ),
                context={"current": current, "min": min_epics},
            )
        return Accepted()

    if op == "delete_ticket":
        target = payload.get("id")
        epic = next(
            (e for e in spec_full.epics if any(t.id == target for t in e.tickets)), None
        )
        if epic is None:
            return Denied(
                code="ticket_not_found",
                message=f"Ticket '{target}' was not found in any epic.",
                context={"id": target},
            )
        if min_tickets is not None and len(epic.tickets) <= min_tickets:
            return Denied(
                code="min_tickets_per_epic_not_met",
                message=(
                    f"Cannot delete this ticket: epic '{epic.id}' must keep at least "
                    f"{min_tickets} ticket(s) (currently {len(epic.tickets)})."
                ),
                context={
                    "epic_id": epic.id,
                    "current": len(epic.tickets),
                    "min": min_tickets,
                },
            )
        return Accepted()

    return Accepted()
