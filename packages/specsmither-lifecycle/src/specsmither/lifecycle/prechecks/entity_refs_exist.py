"""Pre-check: do the entity ids an APS write references actually exist?

Pure. Before any APS write, this fails closed on a dangling foreign-key reference so the
executor never (a) raises a raw SQLite FK ``IntegrityError`` (``create_ticket`` with an
``epicId`` that is not an epic) nor (b) silently no-ops an ``update_``/``delete_`` on an
unknown id (a 0-row write the agent is told "succeeded" — the phantom-success gaslight).
The denial lists the valid ``{id, title}`` roster so a fresh (post-resume) agent is handed
the real ids instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["entity_refs_exist"]


def _roster(items: Any) -> list[dict[str, str]]:
    return [{"id": i.id, "title": getattr(i, "title", "") or ""} for i in items]


def entity_refs_exist(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
) -> PrecheckResult:
    """Deny an APS write whose referenced epic/ticket id does not exist."""
    epics = spec_full.epics
    epic_ids = {e.id for e in epics}
    tickets = [t for e in epics for t in e.tickets]
    ticket_ids = {t.id for t in tickets}

    if op == "create_ticket":
        epic_id = payload.get("epicId")
        if isinstance(epic_id, str) and epic_id not in epic_ids:
            return Denied(
                code="epic_not_found",
                message=(
                    f"Cannot create a ticket under epic '{epic_id}': no such epic in this "
                    "specification. Use one of the listed epic ids."
                ),
                context={"epic_id": epic_id, "valid_epics": _roster(epics)},
            )
    elif op in ("update_epic", "delete_epic"):
        epic_id = payload.get("id")
        if isinstance(epic_id, str) and epic_id not in epic_ids:
            return Denied(
                code="epic_not_found",
                message=(
                    f"Epic '{epic_id}' does not exist in this specification. "
                    "Target one of the listed epic ids."
                ),
                context={"epic_id": epic_id, "valid_epics": _roster(epics)},
            )
    elif op in ("update_ticket", "delete_ticket"):
        ticket_id = payload.get("id")
        if isinstance(ticket_id, str) and ticket_id not in ticket_ids:
            return Denied(
                code="ticket_not_found",
                message=(
                    f"Ticket '{ticket_id}' does not exist in this specification. "
                    "Target one of the listed ticket ids."
                ),
                context={"ticket_id": ticket_id, "valid_tickets": _roster(tickets)},
            )
    elif op == "create_dependencies":
        # broken_reference (2bf6d24f) — a dependency edge endpoint that is not a real
        # ticket would raise a raw FK IntegrityError at commit; deny it up front.
        edges = payload.get("dependencies") or []
        missing = sorted(
            {
                endpoint
                for edge in edges
                if isinstance(edge, Mapping)
                for endpoint in (edge.get("fromTicketId"), edge.get("toTicketId"))
                if isinstance(endpoint, str) and endpoint not in ticket_ids
            }
        )
        if missing:
            return Denied(
                code="broken_reference",
                message=(
                    f"{len(missing)} dependency endpoint(s) reference tickets that do not "
                    f"exist: {', '.join(missing)}. Reference only existing tickets."
                ),
                context={"missing_ticket_ids": missing, "valid_tickets": _roster(tickets)},
            )

    return Accepted()
