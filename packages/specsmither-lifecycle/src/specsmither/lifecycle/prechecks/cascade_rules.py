"""Pre-check: would a delete orphan dependency edges without confirmation?

Faithful port of ``planning/pre-checks/cascade-rules.ts`` (M8.5, A1 §1.4). Pure.

The ticket dependency graph lives on ``SpecFull.dependencies``
(:class:`~specsmither.lifecycle.ports.SpecDependencyEdge` ``{from_ticket_id,
to_ticket_id}`` — ``from`` depends on ``to``). Deleting a ticket (or an epic
whose tickets are depended on from outside) would orphan those edges, so the op
is blocked unless the payload carries explicit ``cascadeRemoveDependencies: true``.

* ``delete_ticket`` — referrers are tickets that depend ON the target
  (``edge.to_ticket_id == target``).
* ``delete_epic`` — referrers are external tickets that depend on any of the
  epic's tickets (``to`` inside the epic, ``from`` outside it).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["cascade_rules"]


def cascade_rules(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
) -> PrecheckResult:
    """Block an un-confirmed delete that would orphan dependency edges."""

    if payload.get("cascadeRemoveDependencies"):
        return Accepted()

    edges = spec_full.dependencies or []
    target = payload.get("id")

    if op == "delete_ticket":
        referrers = [e.from_ticket_id for e in edges if e.to_ticket_id == target]
        if referrers:
            return _cascade_denied("ticket", target, referrers)

    if op == "delete_epic":
        epic = next((e for e in spec_full.epics if e.id == target), None)
        ticket_ids = {t.id for t in (epic.tickets if epic is not None else [])}
        referrers = [
            e.from_ticket_id
            for e in edges
            if e.to_ticket_id in ticket_ids and e.from_ticket_id not in ticket_ids
        ]
        if referrers:
            return _cascade_denied("epic", target, referrers)

    return Accepted()


def _cascade_denied(entity_type: str, target: Any, referrers: list[str]) -> Denied:
    return Denied(
        code="cascade_not_confirmed",
        message=(
            f"Cannot delete {entity_type} '{target}': {len(referrers)} ticket(s) "
            "depend on it. Pass cascadeRemoveDependencies=true to remove the "
            "dependent edges too."
        ),
        context={"entity_type": entity_type, "id": target, "referrer_ids": referrers},
    )
