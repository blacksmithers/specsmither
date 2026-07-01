"""Validator-finding-path → fix-operation mapper (``audit/operation-mapper.ts``).

A **pure**, deterministic lookup that turns a validator finding's ``path`` into the
:data:`~specsmither.lifecycle.operations_registry.PlanningOperationName` that would
*fix* it. It drives the L4 "recommended moves" guidance: each finding surfaced to
the agent is annotated with the concrete verb the agent should call next.

The path schema produced by the crucible validator adapter (verbatim from the TS):

* ``/spec/{field}`` — spec-level rubric finding.
* ``/epics/{entityId}/{field?}`` — epic-level rubric finding.
* ``/tickets/{entityId}/{field?}`` — ticket-level rubric finding.
* ``/blueprints/{entityId}/{field?}`` — blueprint-level rubric finding.
* ``/structural/{field}`` — structural-check finding.
* ``/cross-validation/by-op/{op}`` — cross-validation finding keyed by validator op.

The :data:`_PATTERNS` table is ordered **most-specific-first**: the first matching
entry wins. ``count_below_min`` findings on a ``/structural/{kind}s`` path are
disambiguated by :attr:`MapperContext.category` so they map to the relevant
``create_*`` op rather than the structural ``update_spec`` catch-all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from specsmither.lifecycle.operations_registry import PlanningOperationName

__all__ = [
    "MapperContext",
    "map_path_to_operation",
]


@dataclass(frozen=True)
class MapperContext:
    """Disambiguation context for :func:`map_path_to_operation` (``MapperContext``).

    ``category`` is the validator finding's fine-grained category (e.g.
    ``"count_below_min"``) — *not* the coarse
    :class:`~specsmither.domain.enums.FindingCategory`. It only matters for the
    count/create entries whose ``category_guard`` requires an exact match.
    ``entity_type`` / ``severity`` mirror the TS shape for forward compatibility;
    the current mapping does not read them.
    """

    entity_type: str | None = None
    severity: Literal["finding", "denial"] | None = None
    category: str | None = None


#: ``(compiled pattern, operation, category_guard)`` ordered most-specific-first.
#: A ``category_guard`` of ``None`` matches unconditionally; otherwise the entry
#: only matches when :attr:`MapperContext.category` equals it (ported verbatim from
#: the TS ``PATTERNS`` array).
_PATTERNS: list[tuple[re.Pattern[str], PlanningOperationName, str | None]] = [
    # ---- Count/create signals from the structural layer (count_below_min) ----
    (re.compile(r"^/structural/epics(/|$)"), "create_epic", "count_below_min"),
    (re.compile(r"^/structural/tickets(/|$)"), "create_ticket", "count_below_min"),
    (re.compile(r"^/structural/blueprints(/|$)"), "create_blueprint", "count_below_min"),
    # ---- Cross-validation keyed by validator op ----
    (re.compile(r"^/cross-validation/by-op/link_blueprint"), "link_blueprint_to_tickets", None),
    (re.compile(r"^/cross-validation/by-op/unlink_blueprint"), "unlink_blueprint_to_tickets", None),
    (re.compile(r"^/cross-validation/by-op/add_dependencies"), "create_dependencies", None),
    (re.compile(r"^/cross-validation/by-op/remove_dependency"), "delete_dependencies", None),
    (re.compile(r"^/cross-validation/by-op/create_epic"), "create_epic", None),
    (re.compile(r"^/cross-validation/by-op/update_epic"), "update_epic", None),
    (re.compile(r"^/cross-validation/by-op/delete_epic"), "delete_epic", None),
    (re.compile(r"^/cross-validation/by-op/create_ticket"), "create_ticket", None),
    (re.compile(r"^/cross-validation/by-op/update_ticket"), "update_ticket", None),
    (re.compile(r"^/cross-validation/by-op/delete_ticket"), "delete_ticket", None),
    (re.compile(r"^/cross-validation/by-op/create_blueprint"), "create_blueprint", None),
    (re.compile(r"^/cross-validation/by-op/update_blueprint"), "update_blueprint", None),
    (re.compile(r"^/cross-validation/by-op/delete_blueprint"), "delete_blueprint", None),
    (re.compile(r"^/cross-validation/by-op/set_metadata"), "update_spec", None),
    # Generic cross-validation enrichment fallback (broken-reference / orphan / island).
    (re.compile(r"^/cross-validation/"), "link_blueprint_to_tickets", None),
    # ---- Ticket-level specific sub-paths (must precede the generic ticket pattern) ----
    (
        re.compile(r"^/epics/[^/]+/tickets/[^/]+/dependencies(/|$)"),
        "create_dependencies",
        None,
    ),
    (
        re.compile(r"^/epics/[^/]+/tickets/[^/]+/blueprintReferences(/|$)"),
        "link_blueprint_to_tickets",
        None,
    ),
    # ---- Ticket-level (nested under epics or the top-level /tickets path) ----
    (re.compile(r"^/epics/[^/]+/tickets/"), "update_ticket", None),
    (re.compile(r"^/tickets/"), "update_ticket", None),
    # ---- Epic-level ----
    (re.compile(r"^/epics/"), "update_epic", None),
    # ---- Blueprint-level ----
    (re.compile(r"^/blueprints/"), "update_blueprint", None),
    # ---- Structural catch-all (schema category) ----
    (re.compile(r"^/structural/"), "update_spec", None),
    # ---- Spec-level (after the more-specific spec sub-paths) ----
    (re.compile(r"^/spec(/|$)"), "update_spec", None),
]


def map_path_to_operation(
    path: str,
    context: MapperContext | None = None,
) -> PlanningOperationName | None:
    """Map a validator finding ``path`` to the fix :data:`PlanningOperationName`.

    Returns ``None`` for an empty or unrecognised path. ``context.category``
    disambiguates count findings: a ``/structural/epics`` path with
    ``category="count_below_min"`` maps to ``create_epic`` rather than the
    structural ``update_spec`` catch-all (``mapPathToOperation``, lines 84-97).
    """

    if not path:
        return None

    ctx = context or MapperContext()
    for pattern, operation, category_guard in _PATTERNS:
        if not pattern.search(path):
            continue
        if category_guard is not None and ctx.category != category_guard:
            continue
        return operation

    return None
