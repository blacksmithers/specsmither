"""Pure ticket-status (re)derivation — port of the TS status calculator.

Ports ``operations/src/_internal/status-calculator.ts`` (owner home: the M5.3
cascade aggregator). The TS original threads a ``ticketStore`` /
``ticketDependencyStore`` for I/O; this port keeps only the *decision* logic,
which is pure, and exposes two faithful entry points:

- :func:`should_recalculate` — mirrors ``shouldRecalculateStatus``: a status is
  auto-re-derivable **only** when it is ``pending`` or ``ready``. ``active`` and
  ``done`` are *sticky* — they are user/agent-owned and never auto-flipped.
- :func:`calculate_ticket_status` — the clean ``(status, dep statuses) -> str``
  form the cascade/recompute worklist calls. It collapses the TS branch logic to
  a NOT-EXISTS predicate: a recalculable ticket is ``ready`` iff **every**
  dependency is ``done``, else ``pending``. An unknown/missing dependency is
  represented by the caller as any non-``done`` status string and therefore
  blocks promotion.
- :func:`calculate_status_with_reason` — the richer, TS-faithful shape returning
  the ``{status, reason}`` result (``external_block`` / ``no_dependencies`` /
  ``dependencies_satisfied`` / ``dependencies_unsatisfied``), for callers that
  need the explanatory reason and the list of unsatisfied dependencies.

Determinism: ``done`` is the only "satisfied" status; ordering of
``unsatisfied_deps`` preserves the caller's dependency order (the TS map/filter
is order-preserving). No rounding/float surface here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

CalculatedStatus = Literal["pending", "ready"]
"""The only statuses :func:`calculate_status_with_reason` can *derive*."""

StatusReasonType = Literal[
    "no_dependencies",
    "dependencies_satisfied",
    "dependencies_unsatisfied",
    "external_block",
]

_DONE: Final = "done"
_READY: Final = "ready"
_PENDING: Final = "pending"

# Statuses that are auto-re-derivable. ``active``/``done`` are sticky.
_RECALCULABLE: frozenset[str] = frozenset({_PENDING, _READY})


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """A dependency's status info (TS ``batchGetTicketStatusInfo`` row)."""

    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class UnsatisfiedDependency:
    """A dependency still blocking promotion (status != ``done``)."""

    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class StatusReason:
    """Why a status was derived. Mirrors the TS ``StatusReason`` interface."""

    type: StatusReasonType
    unsatisfied_deps: tuple[UnsatisfiedDependency, ...] | None = None
    block_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StatusCalculationResult:
    """The TS-faithful ``{status, reason}`` derivation result."""

    status: CalculatedStatus
    reason: StatusReason


def should_recalculate(status: str) -> bool:
    """Return ``True`` only for ``pending``/``ready`` (TS ``shouldRecalculateStatus``).

    ``active`` and ``done`` are sticky: they are owned by the work lifecycle and
    are never auto-re-derived from dependency completion.
    """
    return status in _RECALCULABLE


def calculate_ticket_status(
    current_status: str,
    dependency_statuses: Iterable[str],
) -> str:
    """Re-derive a ticket's status from its dependencies' completion.

    Clean entry point for the cascade/recompute worklist:

    - ``active``/``done`` (and any non-recalculable status) are returned
      **unchanged** (sticky).
    - a recalculable ticket (``pending``/``ready``) becomes ``ready`` iff every
      dependency status is ``done`` (vacuously true for no dependencies), else
      ``pending``.

    An unknown/missing dependency is the caller's responsibility to surface as a
    non-``done`` status string; any such value blocks promotion.
    """
    if not should_recalculate(current_status):
        return current_status
    all_done = all(status == _DONE for status in dependency_statuses)
    return _READY if all_done else _PENDING


def calculate_status_with_reason(
    block_reason: str | None,
    dependencies: Iterable[DependencyStatus],
) -> StatusCalculationResult:
    """TS-faithful ``calculateTicketStatus`` decision logic (pure core).

    Mirrors the original branch order exactly:

    1. a truthy ``block_reason`` forces ``pending`` (``external_block``);
    2. no dependencies → ``ready`` (``no_dependencies``);
    3. all dependencies ``done`` → ``ready`` (``dependencies_satisfied``);
    4. otherwise ``pending`` (``dependencies_unsatisfied``) carrying the
       order-preserved list of non-``done`` dependencies.
    """
    if block_reason:
        return StatusCalculationResult(
            status=_PENDING,
            reason=StatusReason(type="external_block", block_reason=block_reason),
        )

    deps = tuple(dependencies)
    if not deps:
        return StatusCalculationResult(
            status=_READY,
            reason=StatusReason(type="no_dependencies"),
        )

    unsatisfied = tuple(d for d in deps if d.status != _DONE)
    if not unsatisfied:
        return StatusCalculationResult(
            status=_READY,
            reason=StatusReason(type="dependencies_satisfied"),
        )

    return StatusCalculationResult(
        status=_PENDING,
        reason=StatusReason(
            type="dependencies_unsatisfied",
            unsatisfied_deps=tuple(
                UnsatisfiedDependency(id=d.id, title=d.title, status=d.status)
                for d in unsatisfied
            ),
        ),
    )


__all__ = [
    "CalculatedStatus",
    "DependencyStatus",
    "StatusCalculationResult",
    "StatusReason",
    "StatusReasonType",
    "UnsatisfiedDependency",
    "calculate_status_with_reason",
    "calculate_ticket_status",
    "should_recalculate",
]
