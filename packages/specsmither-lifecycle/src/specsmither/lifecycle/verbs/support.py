"""Shared verb support — :class:`VerbResult`, the verb errors, and pure helpers.

The four planning verbs (``start`` / ``action`` / ``complete`` / ``inspect``) all
return a :class:`VerbResult` (a composed response + the optional :class:`WritePlan` to
persist). The three error classes are the *throw* path the verbs use when there is no
session to scope a guidance denial to (a missing spec / session, or an SPS precondition
failure); the L5 dispatch facade maps each to an error envelope. The remaining helpers
are the small, pure utilities every verb shares:

* :func:`resolve_now` — freeze ``ports.clock`` once per verb call (so every audit
  timestamp in one verb agrees), returning the instant, its ISO string, and a pinned
  :data:`~specsmither.lifecycle.ports.Clock` to hand the audit / write-plan builders.
* :func:`mint_id` — the ``ports.id_generator`` (ULID) with the ``new_ulid`` fallback.
* :func:`gate_thresholds` — read ``{specification, epic, ticket}`` off the resolved
  validator config for the phase gate.
* :func:`touched_entity_ids` — the flat touched-id collection the gate consumes,
  derived per *effective* phase (spec id in spec/cross-validation, the edited entity
  id in the two expansion phases, ``[]`` for the binary decomposition phases).
* :func:`echo_session` — a transient :class:`PlanningSessionRecord` carrying the
  *post-mutation* phase / status / score the guidance composer echoes back (so a verb
  never mutates the loaded row to shape its response).
* :func:`guidance_snapshot` — the latest-only ``{variant, body}`` guidance blob the
  write plan denormalises onto the session for later ``inspect`` / status display.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from specsmither.domain.enums import PlanningPhase
from specsmither.ids import new_ulid
from specsmither.lifecycle.session_record import PlanningSessionRecord

if TYPE_CHECKING:
    from specsmither.lifecycle.guidance.types import PlanningAgentResponse
    from specsmither.lifecycle.ports import Clock, LifecyclePorts
    from specsmither.lifecycle.write_plan_types import WritePlan

__all__ = [
    "SessionNotFoundError",
    "SpecNotFoundError",
    "SpecNotInPlanningError",
    "VerbResult",
    "echo_session",
    "gate_thresholds",
    "guidance_snapshot",
    "mint_id",
    "resolve_now",
    "touched_entity_ids",
]


# --------------------------------------------------------------------------- #
# Result contract                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerbResult:
    """A verb's output: the composed response + the optional plan to persist.

    ``write_plan`` is ``None`` only for the read-only ``inspect`` verb; every other
    verb (including a denial) carries at least an audit-only plan.
    """

    response: PlanningAgentResponse
    write_plan: WritePlan | None = None


# --------------------------------------------------------------------------- #
# Verb errors (the throw path — no session to scope a denial to)              #
# --------------------------------------------------------------------------- #


class SpecNotFoundError(Exception):
    """SPS could not find the target spec (``SpecNotFoundError``)."""

    def __init__(self, spec_id: str) -> None:
        super().__init__(f"Spec not found: {spec_id}")
        self.spec_id = spec_id


class SessionNotFoundError(Exception):
    """APS / CPS / inspect could not find the session (``SessionNotFoundError``)."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session not found: {session_id}")
        self.session_id = session_id


class SpecNotInPlanningError(Exception):
    """SPS precondition failure — the spec is neither ``draft`` nor ``planning``.

    Thrown (not returned as a guidance denial) because the SPS create path has no
    session to scope a denial to (``SpecNotInPlanningError``).
    """

    def __init__(self, actual_status: str) -> None:
        super().__init__(
            "start_planning_session requires spec status 'draft' or 'planning'; "
            f"spec is '{actual_status}'"
        )
        self.actual_status = actual_status


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #


def _v(value: Any) -> Any:
    """Coerce an :class:`enum.Enum` member to its plain ``.value`` (passthrough else)."""
    return value.value if isinstance(value, Enum) else value


def _as_float(value: Any) -> float:
    """Coerce a numeric threshold to ``float`` (non-numbers → ``0.0``)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def resolve_now(ports: LifecyclePorts) -> tuple[datetime, str, Clock]:
    """Freeze ``ports.clock`` once → ``(now, now_iso, pinned_clock)``.

    Pins the instant so every audit row / write-plan timestamp minted in one verb
    call agrees: read ``ports.clock()`` once at the top, then thread ``now``.
    """
    now = ports.clock() if ports.clock is not None else datetime.now(tz=UTC)
    return now, now.isoformat(), (lambda: now)


def mint_id(ports: LifecyclePorts) -> str:
    """Mint one id via ``ports.id_generator`` (ULID), falling back to ``new_ulid``."""
    generator = ports.id_generator if ports.id_generator is not None else new_ulid
    return generator()


def gate_thresholds(validator_config: Mapping[str, Any]) -> dict[str, float]:
    """Read the ``{specification, epic, ticket}`` gate thresholds from the validator config."""
    thresholds = validator_config.get("thresholds")
    thresholds = thresholds if isinstance(thresholds, Mapping) else {}
    return {
        "specification": _as_float(thresholds.get("specification")),
        "epic": _as_float(thresholds.get("epic")),
        "ticket": _as_float(thresholds.get("ticket")),
    }


def touched_entity_ids(
    payload: Mapping[str, Any] | None, effective_phase: PlanningPhase, spec_id: str
) -> list[str]:
    """The flat touched-id list the phase gate consumes (``extractTouchedEntityIds``).

    Keyed to the mutating op's target (which the effective phase pins down): the spec id
    in ``planning_spec`` (where the only mutating op is ``update_spec``); the edited entity
    id (``payload.id``) in the two ``*_expansion`` phases; ``[]`` everywhere else — the
    binary ``*_decomposition`` phases and ``cross_validation``, whose native ops
    (dependencies / blueprint links) touch no scored entity, so they emit no spec datapoint.
    """
    if effective_phase == PlanningPhase.PLANNING_SPEC:
        return [spec_id]
    if effective_phase in (PlanningPhase.EPIC_EXPANSION, PlanningPhase.TICKET_EXPANSION):
        raw_id = (payload or {}).get("id")
        return [raw_id] if isinstance(raw_id, str) else []
    return []


def echo_session(
    base: PlanningSessionRecord,
    *,
    current_phase: PlanningPhase | str,
    status: str,
    last_score: float | None = None,
    last_gate_result: str | None = None,
    actions_count: int | None = None,
) -> PlanningSessionRecord:
    """A transient :class:`PlanningSessionRecord` carrying the post-mutation echo state.

    The guidance composer derives the response's ``phase`` / ``status`` / cached
    score+gate from a session record; a verb that mutates the session (APS / CPS) builds
    this throwaway record so it never dirties the loaded row to shape its response.
    Fields not overridden fall back to ``base``.
    """
    return PlanningSessionRecord(
        id=base.id,
        specification_id=base.specification_id,
        status=status,
        current_phase=_v(current_phase),
        last_score=last_score if last_score is not None else base.last_score,
        last_gate_result=(
            last_gate_result if last_gate_result is not None else base.last_gate_result
        ),
        pending_human_feedback=base.pending_human_feedback,
        actions_count=actions_count if actions_count is not None else (base.actions_count or 0),
    )


def guidance_snapshot(response: PlanningAgentResponse) -> dict[str, Any]:
    """The latest-only ``{variant, body}`` guidance blob persisted onto the session."""
    return {"variant": response.variant.value, "body": response.guidance}
