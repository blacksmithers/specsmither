"""Pre-check: is an epic cross-referenced by another epic before deletion?

Faithful port of ``planning/pre-checks/cross-cut-references.ts`` (A1 §1.4). Pure.

A ``delete_epic`` is blocked when any **other** epic's serialised JSON contains
the target epic id — the crude substring referrer scan (cross-cut-references.ts:15):
``specFull.epics.filter(e => e.id !== epicId).filter(e => JSON.stringify(e).includes(epicId))``.
This catches an epic whose body (or any of its tickets / extra columns) textually
references the epic about to be deleted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import EpicFull, SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["cross_cut_references"]


def _stringify_epic(epic: EpicFull) -> str:
    """Serialise ``epic`` to JSON for the substring referrer scan.

    Mirrors ``JSON.stringify(e)`` over the full epic row: structured fields +
    nested tickets + the ``extra`` column bag. ``default=str`` keeps any exotic
    value serialisable (the scan only cares about textual containment).
    """

    return json.dumps(asdict(epic), default=str, sort_keys=True)


def cross_cut_references(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
) -> PrecheckResult:
    """Block ``delete_epic`` when other epics textually reference the target id."""

    if op == "delete_epic":
        epic_id = payload.get("id")
        if not isinstance(epic_id, str):
            return Accepted()
        referrers = [
            e.id
            for e in spec_full.epics
            if e.id != epic_id and epic_id in _stringify_epic(e)
        ]
        if referrers:
            return Denied(
                code="cross_cut_reference_exists",
                message=(
                    f"Cannot delete epic '{epic_id}': it is referenced by "
                    f"{len(referrers)} other epic(s)."
                ),
                context={"referrer_ids": referrers},
            )
    return Accepted()
