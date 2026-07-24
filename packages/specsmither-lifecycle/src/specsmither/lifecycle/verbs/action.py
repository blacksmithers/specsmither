"""``action_planning_session`` (APS) — the engine spine.

Pure: ``(payload, ports) -> VerbResult``. The pipeline, in order:

0. ``get_planning_status`` **short-circuits** before every mutation pre-check (the
   poll/resume verb). It never MUTATES the spec, but the ``phase_status_report`` body
   re-validates live so its reported gate matches what ``complete_planning_session``
   would decide (the cached ``last_gate_result`` can be stale); the other poll variants
   compose from the cached state.
1. the pre-check chain — ``operation_allowed`` → ``schema_validate`` → load
   ``spec_full`` → ``spec_status_check('aps')`` → ``count_bounds`` →
   ``cross_cut_references`` → ``cascade_rules`` → ``blueprint_epic_ratio`` → (for
   ``create_dependencies``) the batch dedup/cycle check. The first :class:`Denied`
   short-circuits into an audit-only denial plan.
2. on accept — project the mutation in memory, compute the *effective* phase (a late
   op rewinds to its native phase), re-validate the projected spec, evaluate the
   phase gate, build the audit action (+ a rollback transition for a late op), apply
   the actor-conditional post-status, and assemble the success WritePlan. The verdict
   the agent sees + the persisted ``last_gate_result`` come from the SPEC-WIDE gate
   (the same scope ``complete_planning_session`` / ``get_planning_status`` use, so
   ``action`` never says "gate pass" while ``complete`` denies); the touched-scoped
   gate only drives the per-entity score datapoints.

``payload`` is ``{sessionId, operation, payload?, actor?, userId?}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import ValidationError as PydanticValidationError

from specsmither.domain.enums import (
    ActorType,
    GuidanceVariant,
    Outcome,
    PlanningPhase,
    PlanningSessionStatus,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import build_action, build_transition
from specsmither.lifecycle.config import resolve_lifecycle_config, resolve_validator_config
from specsmither.lifecycle.gate import (
    evaluate_phase_gate,
    evaluate_phase_gate_spec_wide,
)
from specsmither.lifecycle.guidance.compose import (
    compose_get_planning_status,
    compose_response,
    pick_get_planning_status_variant,
)
from specsmither.lifecycle.i18n import resolve_language
from specsmither.lifecycle.operations_registry import (
    OPERATIONS,
    PlanningOperationName,
    get_operation_def,
)
from specsmither.lifecycle.ports import SpecDependencyEdge
from specsmither.lifecycle.prechecks import (
    BatchValidationCycle,
    Denied,
    batch_deduped_denial,
    blueprint_epic_ratio,
    blueprint_link_refs_exist,
    cascade_rules,
    content_shape_valid,
    count_bounds,
    cross_cut_references,
    entity_refs_exist,
    enum_field_guards,
    field_shape_soft_deny,
    operation_allowed,
    run_dependencies_batch,
    schema_validate,
    spec_status_check,
    unknown_field_key,
)
from specsmither.lifecycle.prechecks.cross_val_file_redirect import cross_val_file_redirect
from specsmither.lifecycle.prechecks.strip_field_declarations import (
    strip_agent_wire_field_declarations,
)
from specsmither.lifecycle.verbs.cycle_guidance import build_cycle_detected_denial
from specsmither.lifecycle.verbs.justify_support import JustifyResolution, resolve_justify
from specsmither.lifecycle.verbs.support import (
    SessionNotFoundError,
    VerbResult,
    echo_session,
    gate_thresholds,
    guidance_snapshot,
    resolve_now,
    touched_entity_ids,
)
from specsmither.lifecycle.write_plan import (
    ApsRollback,
    build_aps_denied_write_plan,
    build_aps_success_write_plan,
    build_gps_feedback_consume_write_plan,
    build_gps_read_only_write_plan,
    resolve_aps_mutation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.ports import LifecyclePorts, SpecFull, ValidatorOutput
    from specsmither.lifecycle.session_record import PlanningSessionRecord


__all__ = ["action_planning_session"]

#: Resolved-entity kind → the ``update_*`` op whose mutation machinery justify reuses.
_ENTITY_UPDATE_OP: dict[str, str] = {
    "spec": "update_spec",
    "epic": "update_epic",
    "ticket": "update_ticket",
}


def _justify_mutation(res: JustifyResolution) -> tuple[str, dict[str, Any]]:
    """The synthetic ``update_*`` ``(op, payload)`` a justify/unjustify resolves to — a
    ``SpecMutation`` SET of the merged ``fieldDeclarations`` on the resolved entity."""
    payload: dict[str, Any] = {"fields": {"fieldDeclarations": res.field_declarations}}
    if res.entity_type != "spec":
        payload["id"] = res.entity_id
    return _ENTITY_UPDATE_OP[res.entity_type], payload


def _na_reason_min_length(validator_config: Mapping[str, Any]) -> int:
    """The ``naReason.minLength`` bound (default 20) justify enforces on the reason."""
    na = validator_config.get("naReason")
    if isinstance(na, dict):
        raw = na.get("minLength")
        if isinstance(raw, int):
            return raw
    return 20


def action_planning_session(
    payload: Mapping[str, Any], ports: LifecyclePorts
) -> VerbResult:
    """Run the APS pipeline for ``payload['sessionId']`` → :class:`VerbResult`."""
    session_id = payload["sessionId"]
    # Untrusted client input — validated against OPERATIONS below before use.
    operation = payload["operation"]
    op_payload: Mapping[str, Any] | None = payload.get("payload")
    user_id = payload.get("userId")
    actor = ActorType.HUMAN if payload.get("actor") == "human" else ActorType.AGENT

    session = ports.planning_session_store.get_planning_session(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)

    # Resolve both configs once (project id off the light spec read).
    light_spec = ports.spec_store.get_spec(session.specification_id)
    project_id = light_spec.project_id if light_spec is not None else ""
    lifecycle_config = resolve_lifecycle_config(
        ports.config_store, project_id, session.specification_id,
        default_language=ports.default_language,
    )
    validator_config = resolve_validator_config(
        ports.config_store, project_id, session.specification_id
    )

    # 0. get_planning_status — poll/resume, never mutates (the status-report body
    #    re-validates live so its gate agrees with complete_planning_session).
    if operation == "get_planning_status":
        return _get_planning_status(
            ports,
            session=session,
            op_payload=op_payload,
            user_id=user_id,
            lifecycle_config=lifecycle_config,
            validator_config=validator_config,
        )

    # Reject unknown ops AND the synthetic/audit-only ops (which map to None in OPERATIONS —
    # e.g. an agent mistakenly sending 'complete_planning_session' as an APS operation): both
    # would otherwise crash classify_operation_call with a raw ValueError -> opaque INTERNAL.
    if OPERATIONS.get(operation) is None:
        unknown = Denied(
            code="operation_not_recognised",
            message=(
                f"{operation!r} is not a valid planning operation here. Use a mutating operation "
                "(create_/update_/delete_*), or the complete_planning_session TOOL to finish the phase."
            ),
        )
        return _denied(
            ports, session, operation, op_payload, unknown,
            user_id, lifecycle_config, validator_config,
        )

    current_phase = PlanningPhase(session.current_phase)

    # 1. pre-check chain — first Denied short-circuits.
    op_allowed = operation_allowed(operation, current_phase, op_payload)
    if isinstance(op_allowed, Denied):
        return _denied(ports, session, operation, op_payload, op_allowed, user_id, lifecycle_config, validator_config)
    is_late_op = op_allowed.rollback

    # The agent-wire clean break: strip `fieldDeclarations` from update_*/create_* payloads
    # AFTER operation_allowed has classified the op (rollback decision unchanged) so the key
    # never reaches the persisted mutation NOR the projection. `justify`/`unjustify` own the
    # canonical key and are excluded (no-op here).
    op_payload = strip_agent_wire_field_declarations(operation, op_payload)

    schema_result = schema_validate(operation, op_payload)
    if isinstance(schema_result, Denied):
        return _denied(ports, session, operation, op_payload, schema_result, user_id, lifecycle_config, validator_config)

    # payload-only shape + enum-poison guards (unrecognized fields-map key, off-enum
    # apiContract type, content-less structure, off-enum nfr/guardrail/techStack/goal/
    # requirement values) run before the spec_full load, alongside schema_validate. Without
    # these an unknown key silently no-ops the edit, and an off-enum value crashes the typed
    # write boundary as an opaque INTERNAL error the agent can't recover from.
    for payload_check in (
        unknown_field_key(operation, op_payload or {}),
        field_shape_soft_deny(operation, op_payload or {}),
        enum_field_guards(operation, op_payload or {}),
        content_shape_valid(operation, op_payload or {}),
    ):
        if isinstance(payload_check, Denied):
            return _denied(ports, session, operation, op_payload, payload_check, user_id, lifecycle_config, validator_config)

    spec_full = ports.spec_store.get_spec_full(session.specification_id)
    if spec_full is None:
        raise SessionNotFoundError(session.specification_id)

    status_result = spec_status_check("aps", spec_full.spec.status)
    if isinstance(status_result, Denied):
        return _denied(ports, session, operation, op_payload, status_result, user_id, lifecycle_config, validator_config)

    for check in (
        count_bounds(operation, op_payload or {}, spec_full, validator_config),
        cross_cut_references(operation, op_payload or {}, spec_full),
        # FK-existence guards: deny a write referencing an unknown epic /
        # ticket / blueprint id (raw IntegrityError or phantom-success) with the valid roster.
        entity_refs_exist(operation, op_payload or {}, spec_full),
        blueprint_link_refs_exist(operation, op_payload or {}, spec_full),
        cascade_rules(operation, op_payload or {}, spec_full),
        blueprint_epic_ratio(operation, op_payload or {}, spec_full, validator_config),
    ):
        if isinstance(check, Denied):
            return _denied(ports, session, operation, op_payload, check, user_id, lifecycle_config, validator_config)

    if operation == "create_dependencies":
        incoming = [
            SpecDependencyEdge(
                from_ticket_id=edge["fromTicketId"], to_ticket_id=edge["toTicketId"]
            )
            for edge in (op_payload or {}).get("dependencies", [])
        ]
        batch_result = run_dependencies_batch(incoming, spec_full.dependencies)
        if isinstance(batch_result, BatchValidationCycle):
            # Enrich the bare "remove the offending edge" deny with the per-edge evidence
            # (file-backing + epic/order) + a per-edge recovery plan. The analyzer reads the
            # projected spec's file/order model; create_dependencies only ADDS edges, so the
            # pre-mutation spec == the projected spec for this analysis. The intra-batch-vs-
            # persisted origin tag is computed lifecycle-side from the persisted graph.
            spec_dict = spec_full.spec.model_dump(by_alias=True, exclude_none=True)
            cycle_denial = build_cycle_detected_denial(
                batch_result,
                spec_dict,
                spec_full.dependencies,
                resolve_language(lifecycle_config),
            )
            return _denied(ports, session, operation, op_payload, cycle_denial, user_id, lifecycle_config, validator_config)
        if not batch_result.to_persist:
            return _denied(ports, session, operation, op_payload, batch_deduped_denial(batch_result), user_id, lifecycle_config, validator_config)

    # justify / unjustify — resolve the target entity + validate the scope/reason, producing
    # the FULL merged fieldDeclarations map. The resolution threads into the projection, the
    # SpecMutation, and the touched-entity set below.
    justify_resolution: JustifyResolution | None = None
    if operation in ("justify", "unjustify"):
        jr = resolve_justify(
            operation, op_payload or {}, spec_full, _na_reason_min_length(validator_config)
        )
        if isinstance(jr, Denied):
            return _denied(ports, session, operation, op_payload, jr, user_id, lifecycle_config, validator_config)
        justify_resolution = jr

    # 2. accept — project, score, gate, and assemble the success plan.
    return _accept(
        ports,
        session=session,
        operation=operation,
        op_payload=op_payload,
        spec_full=spec_full,
        current_phase=current_phase,
        is_late_op=is_late_op,
        actor=actor,
        user_id=user_id,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
        justify_resolution=justify_resolution,
    )


# --------------------------------------------------------------------------- #
# get_planning_status short-circuit                                           #
# --------------------------------------------------------------------------- #


def _get_planning_status(
    ports: LifecyclePorts,
    *,
    session: PlanningSessionRecord,
    op_payload: Mapping[str, Any] | None,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, now, clock = resolve_now(ports)

    # The status-report body renders "gate {gate}, score {score}", so it must agree with
    # what complete_planning_session would decide. The cached ``session.last_gate_result``
    # can be stale (e.g. a reject re-activates a session without resetting it), so for that
    # variant we RE-VALIDATE live and route the verdict through the SAME
    # ``evaluate_phase_gate_spec_wide`` the CPS gate uses. Validation is in-process and
    # cheap; the other poll variants keep composing from the cached state.
    spec_full: SpecFull | None = None
    validator_output: ValidatorOutput | None = None
    if pick_get_planning_status_variant(session) == GuidanceVariant.PHASE_STATUS_REPORT:
        spec_full = ports.spec_store.get_spec_full(session.specification_id)
        if spec_full is not None:
            validator_output = ports.validator.validate(
                spec_full,
                PlanningPhase(session.current_phase),
                validator_config,
                language=resolve_language(lifecycle_config),
            )

    composition = compose_get_planning_status(
        session,
        spec_full=spec_full,
        validator_output=validator_output,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )
    snapshot = guidance_snapshot(composition.response)

    action = build_action(
        session_id=session.id,
        operation="get_planning_status",
        phase=session.current_phase,
        outcome=Outcome.SUCCESS,
        actor=ActorType.AGENT,
        guidance_variant=composition.variant,
        payload=op_payload,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    if composition.clear_pending_feedback:
        write_plan = build_gps_feedback_consume_write_plan(
            session_id=session.id, action=action, process_guidance=snapshot
        )
    else:
        write_plan = build_gps_read_only_write_plan(
            session_id=session.id,
            action=action,
            process_guidance=snapshot,
            bump_last_read_at=composition.bump_last_read_at,
            now=now,
        )
    return VerbResult(composition.response, write_plan)


# --------------------------------------------------------------------------- #
# accept                                                                       #
# --------------------------------------------------------------------------- #


def _accept(
    ports: LifecyclePorts,
    *,
    session: PlanningSessionRecord,
    operation: str,
    op_payload: Mapping[str, Any] | None,
    spec_full: SpecFull,
    current_phase: PlanningPhase,
    is_late_op: bool,
    actor: ActorType,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
    justify_resolution: JustifyResolution | None = None,
) -> VerbResult:
    _, _, clock = resolve_now(ports)

    # justify/unjustify reuse the update_* mutation machinery: the resolution targets the
    # entity and carries the merged fieldDeclarations, so the mutation is a SpecMutation SET
    # on that entity. Effective phase + the audit op stay keyed on the original `operation`.
    if justify_resolution is not None:
        mutation_op, mutation_payload = _justify_mutation(justify_resolution)
    else:
        mutation_op, mutation_payload = operation, dict(op_payload or {})

    # The projection validates the mutated content against the typed records. Malformed
    # content (a wrong-shape array item, an off-enum value the proactive guards did not
    # cover) would otherwise raise a raw ValidationError/ValueError that surfaces as an
    # opaque INTERNAL error — leaving the agent blind. Catch it and return a clean,
    # field-level denial so the agent can see exactly what to fix and re-send.
    try:
        projected = ports.operations.apply_mutation(mutation_op, mutation_payload, spec_full)
    except (PydanticValidationError, ValueError) as exc:
        return _denied(
            ports, session, operation, op_payload, _content_denial(exc),
            user_id, lifecycle_config, validator_config,
        )

    # An ordering-only late update_ticket in cross_validation redirects to create_dependencies
    # instead of rolling back (the consumed file exists; only the dependency edge is missing).
    # Findings-conditioned over the projected post-update spec.
    if operation == "update_ticket" and current_phase == PlanningPhase.CROSS_VALIDATION and is_late_op:
        redirect = cross_val_file_redirect(
            operation,
            current_phase,
            op_payload or {},
            projected.spec.model_dump(by_alias=True, exclude_none=True),
            validator_config,
        )
        if isinstance(redirect, Denied):
            return _denied(ports, session, operation, op_payload, redirect, user_id, lifecycle_config, validator_config)

    op_def = get_operation_def(cast(PlanningOperationName, operation))
    native = op_def.native_phase if op_def is not None else None
    effective_phase = (
        native if (is_late_op and isinstance(native, PlanningPhase)) else current_phase
    )

    language = resolve_language(lifecycle_config)
    validator_output = ports.validator.validate(
        projected, effective_phase, validator_config, language=language
    )

    # justify/unjustify touch the resolved entity (its id == spec_id for a spec-justify), so the
    # touched set comes from the resolution, not the op-name/phase-keyed helper.
    touched = (
        [justify_resolution.entity_id]
        if justify_resolution is not None
        else touched_entity_ids(op_payload, effective_phase, session.specification_id)
    )
    gate = evaluate_phase_gate(
        current_phase=effective_phase,
        validator_output=validator_output,
        touched_entity_ids=touched,
        thresholds=gate_thresholds(validator_config),
        all_epic_ids=[epic.id for epic in projected.epics],
        all_ticket_ids=[t.id for epic in projected.epics for t in epic.tickets],
    )
    # The verdict the agent SEES, the persisted ``last_gate_result``, and the guidance's
    # next-entity list must be the SPEC-WIDE per-entity all-pass — the SAME scope
    # ``complete_planning_session`` and ``get_planning_status`` use — so ``action`` never
    # reports "gate pass" (the touched epic cleared) while ``complete`` denies (another
    # epic is still below threshold), which would trap an agent in an edit→complete loop
    # and leave a misleading ``last_gate_result="pass"`` for handover/UI/status-cache
    # consumers. The touched-scoped ``gate`` above still drives ``entity_score_writes``:
    # a score datapoint is stamped only for the entity THIS op actually touched, not a
    # spurious full-refresh of every entity on every edit.
    spec_wide_gate = evaluate_phase_gate_spec_wide(
        current_phase=effective_phase,
        validator_output=validator_output,
        spec_full=projected,
        validator_config=validator_config,
    )

    rollback: ApsRollback | None = None
    if is_late_op:
        transition = build_transition(
            session_id=session.id,
            from_phase=session.current_phase,
            to_phase=effective_phase,
            trigger=TransitionTrigger.AI_AGENT,
            actor=actor,
            id_generator=ports.id_generator,
            clock=clock,
        )
        rollback = ApsRollback(effective_phase=effective_phase, transition=transition)

    prev_session_status = cast(
        Literal["active", "awaiting_human_review"], session.status
    )
    # A HUMAN edit on an awaiting session stays awaiting; otherwise active.
    post_session_status = (
        PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value
        if actor == ActorType.HUMAN
        and prev_session_status == PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value
        else PlanningSessionStatus.ACTIVE.value
    )

    # Variant precedence mirrors resolveVariant: a structural rollback wins; then an edit on an
    # awaiting session (a HUMAN edit keeps it awaiting → handover; an AGENT edit on an awaiting
    # session → feedback); otherwise the gate verdict.
    _awaiting = PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value
    variant = (
        GuidanceVariant.PHASE_ROLLBACK
        if rollback is not None
        else GuidanceVariant.HUMAN_HANDOVER
        if post_session_status == _awaiting
        else GuidanceVariant.HUMAN_FEEDBACK
        if prev_session_status == _awaiting
        else GuidanceVariant.GATE_PASSED
        if spec_wide_gate.gate_outcome == "pass"
        else GuidanceVariant.GATE_FAILED
    )

    response = compose_response(
        variant=variant,
        session=echo_session(
            session,
            current_phase=effective_phase,
            status=post_session_status,
            last_score=validator_output.local_score,
            last_gate_result=spec_wide_gate.gate_outcome,
            actions_count=(session.actions_count or 0) + 1,
        ),
        spec_full=spec_full,
        validator_output=validator_output,
        gate_result=spec_wide_gate,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )

    per_entity_scores_after = {
        **validator_output.per_epic_score,
        **validator_output.per_ticket_score,
    }
    action = build_action(
        session_id=session.id,
        operation=operation,
        phase=session.current_phase,
        outcome=Outcome.SUCCESS,
        actor=actor,
        validator_output=validator_output,
        guidance_variant=response.variant,
        payload=op_payload,
        per_entity_scores_after=per_entity_scores_after,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    mutation = resolve_aps_mutation(
        mutation_op,
        mutation_payload,
        spec_full,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_aps_success_write_plan(
        session_id=session.id,
        mutation=mutation,
        action=action,
        gate_result=spec_wide_gate.gate_outcome,
        validator_output=validator_output,
        entity_score_writes=gate.entity_score_writes,
        prev_session_status=prev_session_status,
        actor=actor,
        rollback=rollback,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)


# --------------------------------------------------------------------------- #
# denial                                                                       #
# --------------------------------------------------------------------------- #


def _content_denial(exc: Exception) -> Denied:
    """Turn a projection ValidationError/ValueError into a clean, field-level denial."""
    if isinstance(exc, PydanticValidationError):
        blockers = [
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg')}"
            for err in exc.errors()[:25]
        ]
        message = (
            f"The submitted content is not valid ({len(blockers)} problem(s)). Correct "
            "these fields and re-send the operation:\n- " + "\n- ".join(blockers)
        )
        return Denied(
            code="invalid_content", message=message, blockers=blockers,
            context={"field_errors": blockers},
        )
    # A non-pydantic ValueError (e.g. an enum coercion: "'x' is not a valid BlueprintCategory").
    text = str(exc)
    return Denied(code="invalid_content", message=text, blockers=[text])


def _denied(
    ports: LifecyclePorts,
    session: PlanningSessionRecord,
    operation: str,
    op_payload: Mapping[str, Any] | None,
    denial: Denied,
    user_id: str | None,
    lifecycle_config: Mapping[str, Any],
    validator_config: Mapping[str, Any],
) -> VerbResult:
    _, _, clock = resolve_now(ports)

    response = compose_response(
        variant=GuidanceVariant.DENIED,
        session=session,
        denial=denial,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )

    action = build_action(
        session_id=session.id,
        operation=operation,
        phase=session.current_phase,
        outcome=Outcome.DENIED,
        actor=ActorType.AGENT,
        guidance_variant=response.variant,
        payload=op_payload,
        deny_reason=denial.code,
        deny_details=denial.context,
        user_id=user_id,
        id_generator=ports.id_generator,
        clock=clock,
    )

    write_plan = build_aps_denied_write_plan(
        session_id=session.id,
        action=action,
        process_guidance=guidance_snapshot(response),
    )
    return VerbResult(response, write_plan)
