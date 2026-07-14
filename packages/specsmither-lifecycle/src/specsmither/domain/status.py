"""Ticket status machine — verbatim port of ``api-types/runtime/status.ts``.

Canonical 4-value status surface matching the published JSON schema
(https://schema.specforge.tech/schema/v1.0/specforge-schema.json). "Blocked" is
not a status — it is signalled via ``block_reason`` on a *pending* ticket.

The spec status vocabulary (8 values) lives in
:class:`specsmither.domain.enums.SpecStatus`; it belongs to the planning
lifecycle, not this entity API surface. This module is deliberately separate
from the status-*calculator* (work item #3, not the calculator).

Predicates accept raw ``str`` and coerce unknown values to
:attr:`TicketStatus.PENDING` (the TS ``asTicketStatus`` fallback), so the
membership predicates always have a defined answer.
"""

from __future__ import annotations

from collections.abc import Mapping

from specsmither.domain.enums import TicketStatus

__all__ = [
    "ACTIONABLE_STATUSES",
    "ACTIVE_STATUSES",
    "BLOCKED_STATUSES",
    "COMPLETE_STATUSES",
    "TICKET_STATUSES",
    "VALID_TRANSITIONS",
    "get_valid_target_statuses",
    "is_actionable_status",
    "is_active_status",
    "is_blocked_status",
    "is_complete_status",
    "is_valid_status",
    "is_valid_transition",
]


TICKET_STATUSES: tuple[TicketStatus, ...] = (
    TicketStatus.PENDING,
    TicketStatus.READY,
    TicketStatus.ACTIVE,
    TicketStatus.DONE,
)

ACTIONABLE_STATUSES: tuple[TicketStatus, ...] = (TicketStatus.READY,)
BLOCKED_STATUSES: tuple[TicketStatus, ...] = (TicketStatus.PENDING,)
ACTIVE_STATUSES: tuple[TicketStatus, ...] = (TicketStatus.ACTIVE,)
COMPLETE_STATUSES: tuple[TicketStatus, ...] = (TicketStatus.DONE,)


VALID_TRANSITIONS: Mapping[TicketStatus, tuple[TicketStatus, ...]] = {
    TicketStatus.PENDING: (TicketStatus.READY, TicketStatus.PENDING),
    TicketStatus.READY: (TicketStatus.ACTIVE, TicketStatus.PENDING),
    TicketStatus.ACTIVE: (TicketStatus.DONE, TicketStatus.PENDING, TicketStatus.READY),
    TicketStatus.DONE: (TicketStatus.ACTIVE, TicketStatus.PENDING, TicketStatus.READY),
}


def is_valid_status(status: str) -> bool:
    """Return ``True`` if ``status`` is one of the four ticket statuses."""
    return status in TICKET_STATUSES


def _as_ticket_status(status: str) -> TicketStatus:
    """Coerce ``status`` to a :class:`TicketStatus`, falling back to ``PENDING``.

    Verbatim port of the internal TS ``asTicketStatus`` helper. The predicates
    below depend on this fallback for unknown input.
    """
    return TicketStatus(status) if is_valid_status(status) else TicketStatus.PENDING


def is_actionable_status(status: str) -> bool:
    """Return ``True`` if ``status`` coerces to an actionable status."""
    return _as_ticket_status(status) in ACTIONABLE_STATUSES


def is_blocked_status(status: str) -> bool:
    """Return ``True`` if ``status`` coerces to a blocked status."""
    return _as_ticket_status(status) in BLOCKED_STATUSES


def is_active_status(status: str) -> bool:
    """Return ``True`` if ``status`` coerces to an active status."""
    return _as_ticket_status(status) in ACTIVE_STATUSES


def is_complete_status(status: str) -> bool:
    """Return ``True`` if ``status`` coerces to a complete status."""
    return _as_ticket_status(status) in COMPLETE_STATUSES


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """Return ``True`` if the (coerced) transition is permitted."""
    return _as_ticket_status(to_status) in VALID_TRANSITIONS[_as_ticket_status(from_status)]


def get_valid_target_statuses(status: str) -> tuple[TicketStatus, ...]:
    """Return the valid target statuses for the (coerced) ``status``."""
    return VALID_TRANSITIONS[_as_ticket_status(status)]
