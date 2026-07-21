"""Pre-check: is the phase gate currently passing? (the CPS gate)

The gate decision is routed through the **phase-gate evaluator** rather than the
validator's composite ``gate_result``, and it **always** re-validates: there is no
cache TTL. Validation is in-process and cheap, so trusting a cached
``session.last_gate_result`` would only add risk (a human edit can break a "fresh"
gate) for no benefit. ``session.last_gate_result`` / ``last_validated_at`` are never
trusted here.

**The CPS gate must use the phase-gate evaluator, not the raw validator
``gate_result``.** The ``last_gate_result`` written on a session comes from
:func:`evaluate_phase_gate`, whose verdict for the two ``*_expansion`` phases is the
spec-wide **per-entity all-pass** (every epic/ticket ≥ its threshold) and
deliberately does **not** fold in the global cascade. The cascade — the weighted
global score including the topology penalty for the ticket dependency DAG — is
enforced only where :func:`evaluate_phase_gate` defers to the validator's
``gate_result``: ``planning_spec``, the ``*_decomposition`` phases, and
**``cross_validation``** (the phase where the DAG is wired). Reading
``output.gate_result`` directly here would wrongly apply the cascade to
``ticket_expansion``, so a spec whose every ticket scores 100 but whose tickets are
still islands (global score 0 before the DAG is wired) could never complete
``ticket_expansion`` — the loop would stall one phase early. Routing through
:func:`evaluate_phase_gate` keeps the phase semantics consistent on every
(always-fresh) re-validation.

The lifecycle ``config`` argument is intentionally absent from the signature (the L4
caller must NOT pass it).

A non-passing gate → :class:`Denied` (``gate_not_passed``) carrying the failing
findings' messages as ``blockers`` so the denial can surface the CURRENT blockers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible.cross_validation.creator_election import elect_file_creators

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.gate import evaluate_phase_gate_spec_wide
from specsmither.lifecycle.guidance.creator_plan_guidance import format_creator_plan
from specsmither.lifecycle.ports import SpecFull, Validator
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult
from specsmither.lifecycle.session_record import PlanningSessionRecord

__all__ = ["gate_currently_passing"]

#: Locator prefix the crucible adapter stamps on file-provenance layer findings — the
#: gate condition for surfacing the creator-election plan (only when the gate is actually
#: failing on orphan-file provenance, so the plan never appears for an unrelated fail).
_FILE_PROVENANCE_PATHS = ("/cross-validation/file-provenance", "/cross-validation/file-conflict")


def _creator_plan_block(spec_full: SpecFull, output: Any, language: str) -> str:
    """The consolidated creator-election plan for a cross_validation file-provenance deny.

    Empty unless the gate carries a file-provenance finding AND there is at least one
    orphan shared file to plan. Uses the strict spec-internal existence model (the
    grep-scoped variant that excludes real-repo brownfield files rides the CPS grep
    double-call, not yet wired here) — so the plan is gated on an actual file-provenance
    finding, keeping it consistent with why the gate failed.
    """
    if not any((f.path or "") in _FILE_PROVENANCE_PATHS for f in output.findings):
        return ""
    spec_dict = spec_full.spec.model_dump(by_alias=True, exclude_none=True)
    return format_creator_plan(elect_file_creators(spec_dict), language)


def _entity_scoreboard(
    phase: PlanningPhase,
    output: Any,
    spec_full: SpecFull,
    validator_config: Mapping[str, Any],
) -> list[str]:
    """A FAIL-first per-entity scoreboard for the scored expansion phases.

    Names each epic/ticket still below its threshold with its score, so the agent knows
    EXACTLY which entities to expand instead of guessing (the single biggest lever for
    converging a phase where entities score high individually but not all clear the gate).
    """
    thresholds = validator_config.get("thresholds", {})
    if phase == PlanningPhase.EPIC_EXPANSION:
        scores = output.per_epic_score
        threshold = thresholds.get("epic", 70)
        titles = {epic.id: getattr(epic, "title", "") for epic in spec_full.epics}
        label = "epic"
    elif phase == PlanningPhase.TICKET_EXPANSION:
        scores = output.per_ticket_score
        threshold = thresholds.get("ticket", 70)
        titles = {
            ticket.id: getattr(ticket, "title", "")
            for epic in spec_full.epics
            for ticket in epic.tickets
        }
        label = "ticket"
    else:
        return []

    below = [
        f"{label} {eid} ({titles.get(eid, '')!r}): scored {round(score, 1)} / needs {threshold} — expand it"
        for eid, score in scores.items()
        if score < threshold
    ]
    if not below:
        return []
    return [
        f"SCOREBOARD — {len(below)} {label}(s) below the {threshold} threshold "
        f"(every {label} must reach it before this phase completes):",
        *below,
    ]


def gate_currently_passing(
    session: PlanningSessionRecord,
    spec_full: SpecFull | None,
    validator: Validator,
    validator_config: Mapping[str, Any],
    language: str = "en",
) -> PrecheckResult:
    """Re-validate the spec at the session's current phase → :class:`Accepted` | :class:`Denied`.

    ALWAYS calls the validator (no cache, no TTL). ``spec_full`` is ``None`` only
    if the spec could not be loaded, in which case the gate cannot be confirmed
    and the call is denied.
    """

    if spec_full is None:
        return Denied(
            code="gate_not_passed",
            message=(
                "Gate cannot be confirmed: the spec could not be loaded. "
                "Continue working on this phase before completing."
            ),
            context={"last_gate_result": session.last_gate_result},
            blockers=[],
        )

    phase = PlanningPhase(session.current_phase)
    output = validator.validate(spec_full, phase, validator_config, language=language)

    # Route the verdict through the phase-gate evaluator (NOT ``output.gate_result``):
    # for the ``*_expansion`` phases the gate is the spec-wide per-entity all-pass and
    # excludes the global cascade, which is enforced only at ``cross_validation``.
    # ``evaluate_phase_gate_spec_wide`` builds the spec-wide scope; the same helper backs
    # the status-report guidance, so the CPS gate and the guidance never disagree.
    gate = evaluate_phase_gate_spec_wide(
        current_phase=phase,
        validator_output=output,
        spec_full=spec_full,
        validator_config=validator_config,
    )

    if gate.gate_outcome != "pass":
        scoreboard = _entity_scoreboard(phase, output, spec_full, validator_config)
        # At cross_validation, PREPEND the consolidated creator-election plan above the flat
        # findings so the actor resolves ALL orphan shared files as ONE structural batch
        # (elect creators in ticket_expansion, declare deps in cross_validation) instead of
        # patching one, rolling back, and rediscovering the rest.
        plan_block = (
            [_creator_plan_block(spec_full, output, language)]
            if phase == PlanningPhase.CROSS_VALIDATION
            else []
        )
        plan_block = [b for b in plan_block if b]
        return Denied(
            code="gate_not_passed",
            message=(
                "Gate is not currently passing. Continue working on this phase "
                "before completing."
            ),
            context={
                "gate_outcome": gate.gate_outcome,
                "gate_result": output.gate_result,
                "phase": phase.value,
            },
            # Lead with the creator-election plan (cross_validation only), then the per-entity
            # scoreboard (which entities are short), then the per-finding blockers (what to add).
            blockers=[*plan_block, *scoreboard, *(f.message for f in output.findings)],
        )

    return Accepted()
