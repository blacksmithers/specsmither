"""The planning state machine — phase order + linear-transition helpers.

Verbatim port of ``planning/state-machine.ts`` (A1 §1.1). Pure: no I/O, no
dependencies beyond the :class:`~specsmither.domain.enums.PlanningPhase`
vocabulary.

The seven phases form a single linear chain::

    planning_spec → epic_decomposition → epic_expansion → ticket_decomposition
        → ticket_expansion → cross_validation → planned

``planned`` is a sentinel/terminal marker, **not** an actionable phase:
``cross_validation`` is the last phase an agent can act in. After the human
approves at ``cross_validation`` the session closes and the spec moves to
``ready``. ``phase_index`` / ``is_later_phase`` drive late-op rollback (an
operation whose native phase precedes the current phase rewinds the session).
"""

from __future__ import annotations

from specsmither.domain.enums import PlanningPhase

__all__ = [
    "PHASE_ORDER",
    "is_later_phase",
    "is_terminal_phase",
    "next_phase",
    "phase_index",
]

#: The seven planning phases in execution order. Mirrors the TS
#: ``PHASE_ORDER`` constant (state-machine.ts:7-10).
PHASE_ORDER: tuple[PlanningPhase, ...] = (
    PlanningPhase.PLANNING_SPEC,
    PlanningPhase.EPIC_DECOMPOSITION,
    PlanningPhase.EPIC_EXPANSION,
    PlanningPhase.TICKET_DECOMPOSITION,
    PlanningPhase.TICKET_EXPANSION,
    PlanningPhase.CROSS_VALIDATION,
    PlanningPhase.PLANNED,
)


def phase_index(phase: PlanningPhase) -> int:
    """Return the 0-based position of ``phase`` in :data:`PHASE_ORDER`, or ``-1``.

    Mirrors ``PHASE_ORDER.indexOf(phase)``. Every :class:`PlanningPhase` member
    is present in :data:`PHASE_ORDER`, so ``-1`` is unreachable for a valid enum
    value; the branch is kept for faithful parity with the TS ``indexOf``.
    """

    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return -1


def next_phase(phase: PlanningPhase) -> PlanningPhase | None:
    """Return the phase after ``phase``, or ``None`` past the end.

    ``None`` when ``phase`` is the last phase (``planned``) or is not a
    recognised phase value. Linear ``index + 1`` (state-machine.ts:16-20).
    """

    idx = phase_index(phase)
    if idx == -1 or idx == len(PHASE_ORDER) - 1:
        return None
    return PHASE_ORDER[idx + 1]


def is_terminal_phase(phase: PlanningPhase) -> bool:
    """Return ``True`` iff ``phase`` is the last *actionable* phase.

    That is ``cross_validation`` — **not** ``planned``. After the human approves
    at ``cross_validation`` the session closes and the spec transitions to
    ``ready``; ``planned`` is a sentinel marker, never an actionable phase
    (state-machine.ts:27-29).
    """

    return phase == PlanningPhase.CROSS_VALIDATION


def is_later_phase(phase: PlanningPhase, than: PlanningPhase) -> bool:
    """Return ``True`` when ``phase`` appears later than ``than`` in the order.

    Drives late-op rollback: an operation native to an earlier phase than the
    session's current phase rewinds the session (state-machine.ts:37-39).
    """

    return phase_index(phase) > phase_index(than)
