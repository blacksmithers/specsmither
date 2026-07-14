"""Pre-check: is the spec in a plannable status for this verb?

Faithful port of ``planning/pre-checks/spec-status-check.ts`` (M7.9, A1 §1.4).
Pure — the caller loads ``spec.status`` and routes a :class:`Denied` through the
standard denial path.

The spec lifecycle is ``draft → planning → ready → in_progress →
ready_for_review → in_review → reviewed → done``. Planning verbs only run while
the spec is being planned:

* ``sps`` (start) accepts ``draft`` (signals an auto-transition to ``planning``)
  or ``planning`` (already there). Any other status is denied.
* ``aps`` (action) / ``cps`` (complete) accept ONLY ``planning``.

Upstream, SPS actually *throws* ``SpecNotInPlanningError`` for a non-plannable
status (there is no session yet to scope a denial); this module models the same
decision uniformly as a :class:`Denied` so the pre-check chain stays homogeneous.
The denial carries :func:`recovery_hint_for_status` so the agent knows the route
back to a plannable state.
"""

from __future__ import annotations

from typing import Literal

from specsmither.domain.enums import SpecStatus
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["SpecStatusVerb", "recovery_hint_for_status", "spec_status_check"]

SpecStatusVerb = Literal["sps", "aps", "cps"]
"""Which planning verb is asking — see :func:`spec_status_check`."""


def spec_status_check(verb: SpecStatusVerb, status: SpecStatus | str) -> PrecheckResult:
    """Check ``status`` against ``verb``'s precondition → :class:`Accepted` | :class:`Denied`.

    On accept, :attr:`Accepted.auto_transition` is ``True`` only for ``sps`` on a
    ``draft`` spec (the SPS verb then flips the spec to ``planning``).
    """

    status_str = str(status)

    if verb == "sps":
        if status_str == SpecStatus.DRAFT.value:
            return Accepted(auto_transition=True)
        if status_str == SpecStatus.PLANNING.value:
            return Accepted(auto_transition=False)
        return _denied_not_in_planning(status_str)

    # aps / cps — only `planning` is valid.
    if status_str == SpecStatus.PLANNING.value:
        return Accepted(auto_transition=False)
    return _denied_not_in_planning(status_str)


def _denied_not_in_planning(status: str) -> Denied:
    hint = recovery_hint_for_status(status)
    return Denied(
        code="spec_not_in_planning",
        message=(
            f"The spec is '{status}', not 'planning'; planning verbs require a spec "
            f"in planning. {hint}"
        ),
        context={"actual_status": status, "recovery_hint": hint},
    )


def recovery_hint_for_status(status: SpecStatus | str) -> str:
    """Per-status recovery hint (``recoveryHintForStatus``, spec-status-check.ts).

    Interpolated into the ``spec_not_in_planning`` denial prose.
    """

    status_str = str(status)
    if status_str == SpecStatus.DRAFT.value:
        return "Call start_planning_session first to begin a planning session."
    if status_str == SpecStatus.PLANNING.value:
        return "The spec is in planning — no recovery needed."
    if status_str == SpecStatus.READY.value:
        return (
            "Planning already completed. Call reopen_specification to return the spec "
            "to planning."
        )
    # in_progress / ready_for_review / in_review / reviewed / done
    return (
        "The spec has advanced into the implementation/review lifecycle; planning "
        "verbs no longer apply."
    )
