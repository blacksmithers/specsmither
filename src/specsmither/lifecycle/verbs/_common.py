"""Shared helpers for the three handover verbs — a pinned clock + a post-state view.

These keep the verbs faithful and DRY without leaking any persistence: a handover verb
reads the session via the port, decides the next state, *builds* a write plan, and
composes the response from a transient post-state view of the session — it never writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from specsmither.db.models import PlanningSession

if TYPE_CHECKING:
    from specsmither.domain.enums import PlanningPhase, PlanningSessionStatus
    from specsmither.lifecycle.ports import Clock, LifecyclePorts

__all__ = [
    "fixed_clock",
    "post_state_view",
    "resolve_now",
]


def resolve_now(ports: LifecyclePorts) -> datetime:
    """The single ``now`` instant for a handover (``ports.clock`` or UTC-now).

    Mirrors the TS ``ports.clock ? ports.clock() : new Date()``. Computed once per verb
    so every audit row + the write plan's session-update timestamps share one instant.
    """

    return ports.clock() if ports.clock is not None else datetime.now(tz=UTC)


def fixed_clock(now: datetime) -> Clock:
    """A constant :class:`~specsmither.lifecycle.ports.Clock` pinned to ``now``.

    Passed to ``build_action`` / ``build_transition`` (which each call ``clock()``
    internally) so all audit timestamps equal the one ``now`` the write plan uses.
    """

    def _clock() -> datetime:
        return now

    return _clock


def post_state_view(
    session: PlanningSession,
    *,
    status: PlanningSessionStatus,
    current_phase: PlanningPhase,
    pending_human_feedback: dict[str, Any] | None,
) -> PlanningSession:
    """A transient :class:`PlanningSession` carrying the POST-mutation state, for composing.

    The verb's write plan persists exactly these fields; the agent response is composed
    from this view so it echoes the *new* phase / status / feedback rather than the
    pre-mutation read. The clone is **never added** to the ORM session (a pure data
    holder, not in the unit of work — it is never flushed), and carries only the handful
    of attributes :func:`~specsmither.lifecycle.guidance.compose.compose_response` reads.
    """

    return PlanningSession(
        id=session.id,
        specification_id=session.specification_id,
        status=status.value,
        current_phase=current_phase.value,
        last_score=session.last_score,
        last_gate_result=session.last_gate_result,
        pending_human_feedback=pending_human_feedback,
        actions_count=session.actions_count,
    )
