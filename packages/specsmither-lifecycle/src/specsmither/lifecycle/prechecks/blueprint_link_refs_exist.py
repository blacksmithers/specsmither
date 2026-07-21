"""Pre-check: do a blueprint-link's referenced ids exist?

Pure. ``link_blueprint_to_tickets`` / ``unlink_blueprint_to_tickets`` write
``ticket_blueprint_refs`` join rows (once the relational write path emits them). This
fails closed BEFORE the write
when the ``blueprintId`` or any ``ticketId`` does not exist, so a dangling id never reaches the
SQLite FK as an ``IntegrityError`` — it becomes a clean soft-deny with the valid rosters.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["blueprint_link_refs_exist"]

_LINK_OPS = frozenset({"link_blueprint_to_tickets", "unlink_blueprint_to_tickets"})


def blueprint_link_refs_exist(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
) -> PrecheckResult:
    """Deny a link/unlink whose blueprint id or any ticket id does not exist."""
    if op not in _LINK_OPS:
        return Accepted()

    blueprint_ids = {b.id for b in spec_full.blueprints}
    ticket_ids = {t.id for e in spec_full.epics for t in e.tickets}

    blueprint_id = payload.get("blueprintId")
    if isinstance(blueprint_id, str) and blueprint_id not in blueprint_ids:
        return Denied(
            code="broken_reference",
            message=(
                f"Blueprint '{blueprint_id}' does not exist in this specification. "
                "Link against one of the listed blueprint ids."
            ),
            context={
                "blueprint_id": blueprint_id,
                "valid_blueprints": [
                    {"id": b.id, "title": getattr(b, "title", "") or ""}
                    for b in spec_full.blueprints
                ],
            },
        )

    ticket_ids_payload = payload.get("ticketIds") or []
    missing = [
        t for t in ticket_ids_payload if isinstance(t, str) and t not in ticket_ids
    ]
    if missing:
        return Denied(
            code="broken_reference",
            message=(
                f"{len(missing)} ticket id(s) in this link do not exist: "
                f"{', '.join(missing)}. Link only existing tickets."
            ),
            context={"missing_ticket_ids": missing, "valid_ticket_ids": sorted(ticket_ids)},
        )

    return Accepted()
