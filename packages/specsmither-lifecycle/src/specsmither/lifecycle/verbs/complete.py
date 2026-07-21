"""``complete_planning_session`` (CPS).

Pure: ``(payload, ports) -> VerbResult``. CPS hands a planned spec to a human:

1. guard ``session.status == 'active'`` (else ``session_not_active`` denial).
2. ``spec_status_check('cps')`` — the spec must be ``planning``.
3. the gate gate — ``gate_currently_passing`` ALWAYS re-validates (no TTL, locked
   decision 3); a non-passing gate denies (``gate_not_passed``) with the current
   blockers.
4. on pass — flip the session to ``awaiting_human_review`` (NOT a phase advance — the
   phase advances only at ``approve_handover``), record a same-phase ``ai_agent``
   transition, and compose the handover-prompt guidance.

``payload`` is ``{sessionId, userId?}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from specsmither.domain.enums import (
    ActorType,
    GuidanceVariant,
    Outcome,
    PlanningSessionStatus,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import build_action, build_transition
from specsmither.lifecycle.config import resolve_lifecycle_config, resolve_validator_config
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.prechecks import Denied, gate_currently_passing, spec_status_check
from specsmither.lifecycle.verbs.support import (
    SessionNotFoundError,
    VerbResult,
    echo_session,
    guidance_snapshot,
    resolve_now,
)
from specsmither.lifecycle.write_plan import (
    build_cps_denied_write_plan,
    build_cps_success_write_plan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.ports import LifecyclePorts
    from specsmither.lifecycle.session_record import PlanningSessionRecord

__all__ = ["complete_planning_session"]

_CPS = "complete_planning_session"


def complete_planning_session(
    payload: Mapping[str, Any], ports: LifecyclePorts
) -> VerbResult:
    """Run CPS for ``payload['sessionId']`` → :class:`VerbResult`."""
    session_id = payload["sessionId"]
    user_id = payload.get("userId")

    session = ports.planning_session_store.get_planning_session(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)

    light_spec = ports.spec_store.get_spec(session.specification_id)
    project_id = light_spec.project_id if light_spec is not None else ""
    lifecycle_config = resolve_lifecycle_config(
        ports.config_store, project_id, session.specification_id
    )
    validator_config = resolve_validator_config(
        ports.config_store, project_id, session.specification_id
    )

    # 1. must be active to complete.
    if session.status != PlanningSessionStatus.ACTIVE.value:
        return _denied(
            ports,
            session,
            Denied(
                code="session_not_active",
                message=(
                    "This session is not active, so it cannot be completed. "
                    f"Its status is '{session.status}'."
                ),
                context={"status": session.status},
            ),
            user_id,
            lifecycle_config,
            validator_config,
        )

    # 2. spec-status precondition — CPS requires the spec in `planning`.
    spec_status = light_spec.status if light_spec is not None else PlanningSessionStatus.ACTIVE.value
    status_result = spec_status_check("cps", spec_status)
    if isinstance(status_result, Denied):
        return _denied(ports, session, status_result, user_id, lifecycle_config, validator_config)

    # 3. the gate gate — ALWAYS re-validates (no TTL).
    spec_full = ports.spec_store.get_spec_full(session.specification_id)
    gate_check = gate_currently_passing(session, spec_full, ports.validator, validator_config)
    if isinstance(gate_check, Denied):
        return _denied(ports, session, gate_check, user_id, lifecycle_config, validator_config)

    # 4. pass — park the session for human review.
    return _success(
        ports,
        session=session,
        user_id=user_id,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )


# --------------------------------------------------------------------------- #
# success                                                                      #
# --------------------------------------------------------------------------- #


def _success(
    ports: LifecyclePorts,
    *,
    session: PlanningSessionRecord,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, _, clock = resolve_now(ports)

    response = compose_response(
        variant=GuidanceVariant.HUMAN_HANDOVER,
        session=echo_session(
            session,
            current_phase=session.current_phase,
            status=PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value,
        ),
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
        # CPS only reaches success once gate_currently_passing accepted — report it as passing,
        # not the pre-mutation cache (which could still read 'fail').
        gate_outcome="pass",
    )

    transition = build_transition(
        session_id=session.id,
        from_phase=session.current_phase,
        to_phase=session.current_phase,
        trigger=TransitionTrigger.AI_AGENT,
        actor=ActorType.AGENT,
        id_generator=ports.id_generator,
        clock=clock,
    )

    action = build_action(
        session_id=session.id,
        operation=_CPS,
        phase=session.current_phase,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        guidance_variant=response.variant,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_cps_success_write_plan(
        session_id=session.id,
        action=action,
        transition=transition,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)


# --------------------------------------------------------------------------- #
# denial                                                                       #
# --------------------------------------------------------------------------- #


def _denied(
    ports: LifecyclePorts,
    session: PlanningSessionRecord,
    denial: Denied,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, _, clock = resolve_now(ports)

    # Report the gate verdict the denial implies, not the pre-mutation cache: a gate denial
    # is a failing gate ('fail'); a session/spec-precondition denial ran no gate (None).
    gate_outcome = "fail" if denial.code == "gate_not_passed" else None
    response = compose_response(
        variant=GuidanceVariant.DENIED,
        session=session,
        denial=denial,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
        gate_outcome=gate_outcome,
    )

    action = build_action(
        session_id=session.id,
        operation=_CPS,
        phase=session.current_phase,
        outcome=Outcome.DENIED,
        actor=ActorType.AGENT,
        guidance_variant=response.variant,
        deny_reason=denial.code,
        deny_details=denial.context,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_cps_denied_write_plan(
        session_id=session.id,
        action=action,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)
