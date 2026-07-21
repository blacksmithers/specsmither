"""``start_planning_session`` (SPS).

Pure: ``(payload, ports) -> VerbResult``. SPS reads the spec, checks the status
precondition, resolves both configs, then branches on the active session:

* **none → create** — mint a session (``active`` / ``planning_spec`` / trigger
  ``auto_initial``), flipping a ``draft`` spec to ``planning`` in the same plan.
* **active → resume** — idempotent re-entry. Per locked decision 3 the gate cache is
  **never** trusted: SPS resume ALWAYS re-validates (in-process, cheap) and persists
  the fresh output, rather than reading a phase-keyed cache.
* **awaiting_human_review → deny** — SPS is not for an awaiting session; it denies
  (``sps_not_for_awaiting``) and points the agent at ``get_planning_status``.

``payload`` is ``{specId, userId?}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from specsmither.domain.enums import (
    ActorType,
    GuidanceVariant,
    Outcome,
    PlanningPhase,
    PlanningSessionStatus,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import build_action
from specsmither.lifecycle.config import resolve_lifecycle_config, resolve_validator_config
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.i18n import resolve_language
from specsmither.lifecycle.prechecks import Denied, spec_status_check
from specsmither.lifecycle.session_record import PlanningSessionRecord
from specsmither.lifecycle.verbs.support import (
    SpecNotFoundError,
    SpecNotInPlanningError,
    VerbResult,
    guidance_snapshot,
    mint_id,
    resolve_now,
)
from specsmither.lifecycle.write_plan import (
    build_sps_create_write_plan,
    build_sps_deny_awaiting_write_plan,
    build_sps_resume_write_plan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = ["start_planning_session"]

_SPS = "start_planning_session"


def start_planning_session(
    payload: Mapping[str, Any], ports: LifecyclePorts
) -> VerbResult:
    """Run SPS for ``payload['specId']`` → :class:`VerbResult`."""
    spec_id = payload["specId"]
    user_id = payload.get("userId")

    spec = ports.spec_store.get_spec(spec_id)
    if spec is None:
        raise SpecNotFoundError(spec_id)

    spec_status = spec.status
    if isinstance(spec_status_check("sps", spec_status), Denied):
        # No session to scope a denial to → throw (mirrors SpecNotInPlanningError).
        raise SpecNotInPlanningError(str(spec_status))

    project_id = spec.project_id
    lifecycle_config = resolve_lifecycle_config(
        ports.config_store, project_id, spec_id, default_language=ports.default_language
    )
    validator_config = resolve_validator_config(ports.config_store, project_id, spec_id)

    active = ports.planning_session_store.get_active_planning_session_by_spec(spec_id)

    if active is None:
        return _create(
            ports,
            spec_id=spec_id,
            spec_status=spec_status,
            user_id=user_id,
            lifecycle_config=lifecycle_config,
            validator_config=validator_config,
        )
    if active.status == PlanningSessionStatus.ACTIVE.value:
        return _resume(
            ports,
            session=active,
            user_id=user_id,
            lifecycle_config=lifecycle_config,
            validator_config=validator_config,
        )
    return _deny_awaiting(
        ports,
        session=active,
        user_id=user_id,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )


# --------------------------------------------------------------------------- #
# CREATE                                                                       #
# --------------------------------------------------------------------------- #


def _create(
    ports: LifecyclePorts,
    *,
    spec_id: str,
    spec_status: Any,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, now_iso, clock = resolve_now(ports)
    session_id = mint_id(ports)

    session_fields: dict[str, Any] = {
        "id": session_id,
        "specification_id": spec_id,
        "status": PlanningSessionStatus.ACTIVE.value,
        "current_phase": PlanningPhase.PLANNING_SPEC.value,
        "started_at": now_iso,
        "last_action_at": now_iso,
        "actions_count": 0,
        "last_transition_trigger": TransitionTrigger.AUTO_INITIAL.value,
        "last_transition_at": now_iso,
        "last_read_at": now_iso,
    }
    session_row = PlanningSessionRecord(**session_fields)

    response = compose_response(
        variant=GuidanceVariant.PHASE_STATUS_REPORT,
        session=session_row,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )

    action = build_action(
        session_id=session_id,
        operation=_SPS,
        phase=PlanningPhase.PLANNING_SPEC,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        guidance_variant=response.variant,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_sps_create_write_plan(
        spec_id=spec_id,
        spec_status=spec_status,
        session={**session_fields, "last_process_guidance": guidance_snapshot(response)},
        action=action,
    )
    return VerbResult(response, write_plan)


# --------------------------------------------------------------------------- #
# RESUME-active                                                                #
# --------------------------------------------------------------------------- #


def _resume(
    ports: LifecyclePorts,
    *,
    session: PlanningSessionRecord,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, now_iso, clock = resolve_now(ports)
    phase = PlanningPhase(session.current_phase)

    # Locked decision 3: never trust the cache on resume — always re-validate.
    spec_full = ports.spec_store.get_spec_full(session.specification_id)
    language = resolve_language(lifecycle_config)
    validator_output = (
        ports.validator.validate(spec_full, phase, validator_config, language=language)
        if spec_full is not None
        else None
    )

    response = compose_response(
        variant=GuidanceVariant.PHASE_STATUS_REPORT,
        session=session,
        spec_full=spec_full,
        validator_output=validator_output,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )

    action = build_action(
        session_id=session.id,
        operation=_SPS,
        phase=phase,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        validator_output=validator_output,
        guidance_variant=response.variant,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_sps_resume_write_plan(
        session_id=session.id,
        action=action,
        fresh_validator_output=validator_output,
        fresh_validated_at=now_iso if validator_output is not None else None,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)


# --------------------------------------------------------------------------- #
# DENY-awaiting                                                                #
# --------------------------------------------------------------------------- #


def _deny_awaiting(
    ports: LifecyclePorts,
    *,
    session: PlanningSessionRecord,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, _, clock = resolve_now(ports)
    denial = Denied(
        code="sps_not_for_awaiting",
        message=(
            "This session is awaiting human review. Call get_planning_status to poll "
            "for the human decision and receive resume guidance."
        ),
    )

    response = compose_response(
        variant=GuidanceVariant.DENIED,
        session=session,
        denial=denial,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )

    action = build_action(
        session_id=session.id,
        operation=_SPS,
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

    write_plan = build_sps_deny_awaiting_write_plan(
        session_id=session.id,
        action=action,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)
