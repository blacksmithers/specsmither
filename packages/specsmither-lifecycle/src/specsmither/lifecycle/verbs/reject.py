"""``reject_handover`` — the human rejects a parked session, no written feedback.

A separate (non-dispatch) entrypoint. Flips
the session back to ``'active'`` with ``last_transition_trigger = 'human_reject_no_feedback'``
(the phase is unchanged); the agent learns on its next ``get_planning_status`` poll to ask
the human in chat what needs rework. Pure ``(payload, ports) -> HandoverOutcome``; the
actor is always ``human``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from specsmither.domain.enums import (
    ActorType,
    GuidanceVariant,
    Outcome,
    PlanningPhase,
    PlanningSessionStatus,
)
from specsmither.lifecycle.audit import build_action
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.verbs._common import fixed_clock, post_state_view, resolve_now
from specsmither.lifecycle.verbs.types import (
    HandoverError,
    HandoverOutcome,
    HandoverResult,
    RejectHandoverPayload,
)
from specsmither.lifecycle.write_plan import build_reject_handover_write_plan

if TYPE_CHECKING:
    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = ["reject_handover"]


def reject_handover(payload: RejectHandoverPayload, ports: LifecyclePorts) -> HandoverOutcome:
    """Reject a parked handover with no feedback — flip the session back to ``active``."""

    now = resolve_now(ports)
    clock = fixed_clock(now)

    session = ports.planning_session_store.get_planning_session(payload.session_id)
    if session is None:
        return HandoverError(
            code="SESSION_NOT_FOUND",
            message=f"Session not found: {payload.session_id}",
        )
    if session.status != PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value:
        return HandoverError(
            code="HANDOVER_NOT_PENDING",
            message=(
                f"Session {session.id} is not awaiting human review "
                f"(status={session.status})."
            ),
        )

    current_phase = PlanningPhase(session.current_phase)
    action = build_action(
        session_id=session.id,
        operation="human_reject_no_feedback",
        phase=current_phase,
        outcome=Outcome.SUCCESS,
        actor=ActorType.HUMAN,
        user_id=payload.user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )
    write_plan = build_reject_handover_write_plan(
        session_id=session.id, action=action, now=now
    )
    view = post_state_view(
        session,
        status=PlanningSessionStatus.ACTIVE,
        current_phase=current_phase,
        pending_human_feedback=session.pending_human_feedback,
    )
    response = compose_response(
        variant=GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK, session=view
    )
    return HandoverResult(
        session_id=session.id,
        new_status=PlanningSessionStatus.ACTIVE,
        new_phase=current_phase,
        response=response,
        write_plan=write_plan,
    )
