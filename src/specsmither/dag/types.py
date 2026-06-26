"""Shared input vocabulary for the pure DAG engine.

Minimal, immutable views the recompute worklist (``rollups/recompute.py``)
builds from the authoritative DB rows and hands to the pure aggregators
(``status_calculator``, ``cascade``, ``critical_path``, ``tree``). Keeping the
inputs ORM-free keeps the aggregators pure and trivially testable.

Outputs — ``CascadeTransition``, ``TreeTicket``/``DependencyTree``,
``CriticalPathNode`` — live in the module that produces them.

Determinism note: ids are assumed ASCII (ULID/UUID), so Python ``sorted()``
matches the TS engine's JS UTF-16 ordering byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TicketNode:
    """A ticket as the DAG sees it (a row projection, not the ORM model)."""

    id: str
    status: str
    estimated_minutes: int | None = None
    epic_id: str | None = None
    epic_number: int | None = None
    ticket_number: int | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class EpicNode:
    """An epic's identity/ordering metadata for the dependency tree."""

    id: str
    epic_number: int | None = None
    title: str | None = None
    order: int | None = None


@dataclass(frozen=True, slots=True)
class DepEdge:
    """A dependency edge: ``ticket_id`` depends on ``depends_on_id``.

    Mirrors the TS ``TicketDependency`` row exactly (``ticketId`` /
    ``dependsOnId``): the *dependent* end is ``ticket_id``, the *blocker* end is
    ``depends_on_id``. ``find_dependents(X)`` = edges whose ``depends_on_id == X``.
    """

    ticket_id: str
    depends_on_id: str
    type: str = "requires"


__all__ = ["DepEdge", "EpicNode", "TicketNode"]
