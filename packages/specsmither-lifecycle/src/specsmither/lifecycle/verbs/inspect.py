"""``inspect_planning_session`` — ported from
``planning/verbs/inspect-planning-session.ts`` (A1 §1.5).

Pure and **read-only**: ``(payload, ports) -> VerbResult`` with ``write_plan = None``.
Inspect composes a status report from the session, its (optional) ``spec_full``, and
the PERSISTED ``last_validator_output`` blob — surfaced for display only, never
re-validated as a cache. The persisted blob is rehydrated into a
:class:`~specsmither.lifecycle.ports.ValidatorOutput` so the composer can show the
last score / gate / findings exactly as they were recorded.

``payload`` is ``{sessionId}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from specsmither.domain.enums import GuidanceVariant, PlanningPhase
from specsmither.lifecycle.config import resolve_lifecycle_config, resolve_validator_config
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.ports import ValidatorFinding, ValidatorOutput
from specsmither.lifecycle.verbs.support import SessionNotFoundError, VerbResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = ["inspect_planning_session"]


def inspect_planning_session(
    payload: Mapping[str, Any], ports: LifecyclePorts
) -> VerbResult:
    """Compose a read-only status/inspect response for ``payload['sessionId']``."""
    session_id = payload["sessionId"]

    session = ports.planning_session_store.get_planning_session(session_id)
    if session is None:
        raise SessionNotFoundError(session_id)

    spec_full = ports.spec_store.get_spec_full(session.specification_id)
    project_id = spec_full.spec.project_id if spec_full is not None else ""
    lifecycle_config = resolve_lifecycle_config(
        ports.config_store, project_id, session.specification_id
    )
    validator_config = resolve_validator_config(
        ports.config_store, project_id, session.specification_id
    )

    cached_output = _validator_output_from_blob(session.last_validator_output)

    response = compose_response(
        variant=GuidanceVariant.PHASE_STATUS_REPORT,
        session=session,
        spec_full=spec_full,
        validator_output=cached_output,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )
    # Read-only: no WritePlan.
    return VerbResult(response, None)


def _validator_output_from_blob(blob: Any) -> ValidatorOutput | None:
    """Rehydrate a persisted ``last_validator_output`` blob → :class:`ValidatorOutput`.

    The blob shape is the one :func:`~specsmither.lifecycle.write_plan._serialize_validator_output`
    persists. A missing / malformed blob (or unknown ``validated_phase``) yields
    ``None`` — the composer then falls back to the session's cached ``last_score`` /
    ``last_gate_result``.
    """
    if not isinstance(blob, dict):
        return None
    try:
        phase = PlanningPhase(blob["validated_phase"])
    except (KeyError, ValueError):
        return None

    gate_result = blob.get("gate_result")
    if gate_result not in ("pass", "fail"):
        return None

    findings = [
        ValidatorFinding(
            category=item.get("category", ""),
            message=item.get("message", ""),
            severity=item.get("severity", "finding"),
            entity_id=item.get("entity_id"),
            entity_type=item.get("entity_type"),
            path=item.get("path"),
            points_lost=item.get("points_lost"),
            global_impact_on_fix=item.get("global_impact_on_fix"),
        )
        for item in blob.get("findings", [])
        if isinstance(item, dict)
    ]

    return ValidatorOutput(
        gate_result=gate_result,
        local_score=float(blob.get("local_score") or 0.0),
        per_epic_score=dict(blob.get("per_epic_score") or {}),
        per_ticket_score=dict(blob.get("per_ticket_score") or {}),
        findings=findings,
        validated_phase=phase,
    )
