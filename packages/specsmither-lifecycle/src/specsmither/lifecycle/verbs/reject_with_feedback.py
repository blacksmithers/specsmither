"""``reject_handover_with_feedback`` — reject a parked session, stash feedback (A1 §4.2).

Ported from ``verbs/reject-handover-with-feedback.ts``. A separate (non-dispatch)
entrypoint. Validates that ``feedback`` is a non-empty string (else ``INVALID_FEEDBACK``),
flips the session back to ``'active'`` and stashes ``pending_human_feedback`` =
``{content, recorded_at, recorded_by_user_id}`` for one-shot delivery to the agent on its
next ``get_planning_status`` poll; ``last_transition_trigger = 'human_reject_with_feedback'``
and the audit action carries ``human_instruction = feedback``. The phase is unchanged. Pure
``(payload, ports) -> HandoverOutcome``; the actor is always ``human``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    RejectHandoverWithFeedbackPayload,
)
from specsmither.lifecycle.write_plan import build_reject_handover_with_feedback_write_plan

if TYPE_CHECKING:
    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = ["reject_handover_with_feedback"]


def reject_handover_with_feedback(
    payload: RejectHandoverWithFeedbackPayload, ports: LifecyclePorts
) -> HandoverOutcome:
    """Reject a parked handover with feedback — flip to ``active`` and stash the feedback."""

    now = resolve_now(ports)
    clock = fixed_clock(now)

    if not isinstance(payload.feedback, str) or not payload.feedback.strip():
        return HandoverError(
            code="INVALID_FEEDBACK",
            message="feedback must be a non-empty string.",
        )

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
    feedback: dict[str, Any] = {
        "content": payload.feedback,
        "recorded_at": now.isoformat(),
        "recorded_by_user_id": payload.user_id,
    }
    action = build_action(
        session_id=session.id,
        operation="human_reject_with_feedback",
        phase=current_phase,
        outcome=Outcome.SUCCESS,
        actor=ActorType.HUMAN,
        human_instruction=payload.feedback,
        user_id=payload.user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )
    write_plan = build_reject_handover_with_feedback_write_plan(
        session_id=session.id, action=action, feedback=feedback, now=now
    )
    view = post_state_view(
        session,
        status=PlanningSessionStatus.ACTIVE,
        current_phase=current_phase,
        pending_human_feedback=feedback,
    )
    response = compose_response(variant=GuidanceVariant.HUMAN_FEEDBACK, session=view)
    return HandoverResult(
        session_id=session.id,
        new_status=PlanningSessionStatus.ACTIVE,
        new_phase=current_phase,
        response=response,
        write_plan=write_plan,
    )
