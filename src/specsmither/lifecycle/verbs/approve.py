"""``approve_handover`` — the human approves a parked planning session (A1 §4.1).

Ported from ``verbs/approve-handover.ts``. A separate (non-dispatch) entrypoint: the
human, having reviewed the spec parked by ``complete_planning_session``, approves the
handover. Pure ``(payload, ports) -> HandoverOutcome``.

The flow:

1. Guard ``session.status == 'awaiting_human_review'`` (else ``HANDOVER_NOT_PENDING``).
2. **M11.1 gate-on-pass precondition**: ``session.last_gate_result != 'pass'`` →
   ``HANDOVER_GATE_FAILING``. ``None`` (undefined) is treated as NOT passing — the
   original awaiting entry implies a recorded pass, so an absent value is a gap, not an
   implicit pass. A human/agent edit that broke the gate while still awaiting must
   re-pass (or be rejected-with-feedback) before approval.
3. **Terminal split** on :func:`is_terminal_phase` (``cross_validation``):
   * TERMINAL → spec status ``'ready'`` + session ``'closed'`` (with ``closed_at``);
     audit ``[phase_complete, session_closed]``. **This is the only path to spec
     ``'ready'``.**
   * NON-TERMINAL → advance to ``next_phase`` (or the ``planned`` sentinel), session
     back to ``'active'``; a ``human_approve`` transition is recorded; audit
     ``[phase_advance, human_approve]``.

The actor is always ``human``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from specsmither.domain.enums import (
    ActorType,
    GuidanceVariant,
    Outcome,
    PlanningPhase,
    PlanningSessionStatus,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import build_action, build_transition
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.state_machine import is_terminal_phase, next_phase
from specsmither.lifecycle.verbs._common import fixed_clock, post_state_view, resolve_now
from specsmither.lifecycle.verbs.types import (
    ApproveHandoverPayload,
    HandoverError,
    HandoverOutcome,
    HandoverResult,
)
from specsmither.lifecycle.write_plan import build_approve_handover_write_plan

if TYPE_CHECKING:
    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = ["approve_handover"]


def approve_handover(payload: ApproveHandoverPayload, ports: LifecyclePorts) -> HandoverOutcome:
    """Approve a parked handover — advance the phase, or close the session at the terminal."""

    now = resolve_now(ports)
    clock = fixed_clock(now)
    id_generator = ports.id_generator

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
    # M11.1 — approve only on a passing gate; None/undefined is NOT passing.
    if session.last_gate_result != "pass":
        return HandoverError(
            code="HANDOVER_GATE_FAILING",
            message=(
                f"Session {session.id} cannot be approved while the phase gate is failing "
                f"(last_gate_result={session.last_gate_result or 'none'}). Keep editing "
                "until the gate re-passes, or reject with feedback."
            ),
        )

    current_phase = PlanningPhase(session.current_phase)

    if is_terminal_phase(current_phase):
        actions = [
            build_action(
                session_id=session.id,
                operation="phase_complete",
                phase=current_phase,
                outcome=Outcome.SUCCESS,
                actor=ActorType.HUMAN,
                user_id=payload.user_id,
                id_generator=id_generator,
                clock=clock,
            ),
            build_action(
                session_id=session.id,
                operation="session_closed",
                phase=current_phase,
                outcome=Outcome.SUCCESS,
                actor=ActorType.HUMAN,
                user_id=payload.user_id,
                id_generator=id_generator,
                clock=clock,
            ),
        ]
        write_plan = build_approve_handover_write_plan(
            session_id=session.id,
            spec_id=session.specification_id,
            transitioned_phase=current_phase,
            is_terminal=True,
            now=now,
            actions=actions,
        )
        view = post_state_view(
            session,
            status=PlanningSessionStatus.CLOSED,
            current_phase=current_phase,
            pending_human_feedback=session.pending_human_feedback,
        )
        response = compose_response(variant=GuidanceVariant.SESSION_CLOSED, session=view)
        return HandoverResult(
            session_id=session.id,
            new_status=PlanningSessionStatus.CLOSED,
            new_phase=current_phase,
            response=response,
            write_plan=write_plan,
        )

    nxt = next_phase(current_phase)
    advanced_phase = nxt if nxt is not None else PlanningPhase.PLANNED
    transition = build_transition(
        session_id=session.id,
        from_phase=current_phase,
        to_phase=advanced_phase,
        trigger=TransitionTrigger.HUMAN_APPROVE,
        actor=ActorType.HUMAN,
        id_generator=id_generator,
        clock=clock,
    )
    actions = [
        build_action(
            session_id=session.id,
            operation="phase_advance",
            phase=current_phase,
            outcome=Outcome.SUCCESS,
            actor=ActorType.HUMAN,
            user_id=payload.user_id,
            id_generator=id_generator,
            clock=clock,
        ),
        build_action(
            session_id=session.id,
            operation="human_approve",
            phase=current_phase,
            outcome=Outcome.SUCCESS,
            actor=ActorType.HUMAN,
            user_id=payload.user_id,
            id_generator=id_generator,
            clock=clock,
        ),
    ]
    write_plan = build_approve_handover_write_plan(
        session_id=session.id,
        spec_id=session.specification_id,
        transitioned_phase=advanced_phase,
        is_terminal=False,
        now=now,
        actions=actions,
        transition=transition,
    )
    view = post_state_view(
        session,
        status=PlanningSessionStatus.ACTIVE,
        current_phase=advanced_phase,
        pending_human_feedback=session.pending_human_feedback,
    )
    response = compose_response(variant=GuidanceVariant.PHASE_ADVANCE, session=view)
    return HandoverResult(
        session_id=session.id,
        new_status=PlanningSessionStatus.ACTIVE,
        new_phase=advanced_phase,
        response=response,
        write_plan=write_plan,
    )
