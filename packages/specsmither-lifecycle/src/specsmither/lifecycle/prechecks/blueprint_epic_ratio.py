"""Pre-check: would deleting a blueprint breach the blueprint:epic ratio?

Pure. The **validator** config (domain ``planning``) owns the ratio. The lifecycle
``delete_blueprint`` hard-deny reads it off
``ValidatorConfig.crossValidation.ratios.blueprintToEpic.min`` and blocks the
delete when ``(blueprintCount - 1) / epicCount < ratio`` (with ``epicCount > 0``).
``create_blueprint`` is never blocked here — the ratio is a delete-side hard-deny
only (no create-side max in M2).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["blueprint_epic_ratio"]


def _blueprint_to_epic_min(config: Mapping[str, Any]) -> float | None:
    cross = config.get("crossValidation")
    ratios = cross.get("ratios") if isinstance(cross, Mapping) else None
    bte = ratios.get("blueprintToEpic") if isinstance(ratios, Mapping) else None
    value = bte.get("min") if isinstance(bte, Mapping) else None
    return float(value) if isinstance(value, int | float) else None


def blueprint_epic_ratio(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
    spec_full: SpecFull,
    config: Mapping[str, Any],
) -> PrecheckResult:
    """Block ``delete_blueprint`` when the post-delete ratio falls below the minimum."""

    if op != "delete_blueprint":
        return Accepted()

    ratio = _blueprint_to_epic_min(config)
    if ratio is None:
        return Accepted()

    epic_count = len(spec_full.epics)
    blueprint_count = len(spec_full.blueprints)
    after_delete = blueprint_count - 1

    if epic_count > 0 and after_delete / epic_count < ratio:
        min_required = math.ceil(epic_count * ratio)
        return Denied(
            code="blueprint_epic_ratio_violated",
            message=(
                f"Cannot delete this blueprint: {after_delete} blueprint(s) for "
                f"{epic_count} epic(s) would fall below the required blueprint:epic "
                f"ratio (need at least {min_required})."
            ),
            context={
                "current": blueprint_count,
                "epics": epic_count,
                "min_required": min_required,
                "after_delete": after_delete,
            },
        )
    return Accepted()
