"""Pre-check: is the phase gate currently passing? (the CPS gate)

Port of ``planning/pre-checks/gate-currently-passing.ts`` (A1 §1.4, §3.3) with
the cache TTL **removed** (locked decision 3: ALWAYS re-validate) and the gate
decision routed through the **phase-gate evaluator** rather than the validator's
composite ``gate_result``.

The TS source trusts ``session.lastGateResult == 'pass'`` while a 5-minute TTL
(``gateResultCacheTtlMs``) is unexpired, re-validating only when stale or
non-passing. Locally, validation is in-process and cheap, so the staleness knob
adds risk (a human edit can break a "fresh" gate) for no benefit: this port
**drops** ``Date.now()`` / ``gateResultCacheTtlMs`` entirely and always
re-validates. ``session.last_gate_result`` / ``last_validated_at`` are never
trusted here.

**Simulator find (2026-07-13): the CPS gate must use the phase-gate evaluator,
not the raw validator ``gate_result``.** The TS ``lastGateResult`` the cache
trusts is written by ``evaluatePhaseGate`` (:func:`evaluate_phase_gate`), whose
verdict for the two ``*_expansion`` phases is the spec-wide **per-entity
all-pass** (every epic/ticket ≥ its threshold) and deliberately does **not**
fold in the global cascade. The cascade — the weighted global score including the
topology penalty for the ticket dependency DAG — is enforced only where
``evaluatePhaseGate`` defers to ``validator.gateResult``: ``planning_spec``, the
``*_decomposition`` phases, and **``cross_validation``** (the phase where the
persona wires the DAG). Reading ``output.gate_result`` directly here — as this
port originally did — wrongly applies the cascade to ``ticket_expansion``, so a
spec whose every ticket scores 100 but whose tickets are still islands (global
score 0 before the DAG is wired) can never complete ``ticket_expansion`` — the
loop stalls one phase early. Routing through :func:`evaluate_phase_gate` restores
the SpecForge semantics on every (always-fresh) re-validation.

Because the lifecycle ``config`` argument existed solely to carry the now-removed
TTL, it is dropped from the signature (the L4 caller must NOT pass it).

A non-passing gate → :class:`Denied` (``gate_not_passed``) carrying the failing
findings' messages as ``blockers`` so the denial can surface the CURRENT blockers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.gate import evaluate_phase_gate_spec_wide
from specsmither.lifecycle.ports import SpecFull, Validator
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult
from specsmither.lifecycle.session_record import PlanningSessionRecord

__all__ = ["gate_currently_passing"]


def _entity_scoreboard(
    phase: PlanningPhase,
    output: Any,
    spec_full: SpecFull,
    validator_config: Mapping[str, Any],
) -> list[str]:
    """MB.1.4 — a FAIL-first per-entity scoreboard for the scored expansion phases.

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
    output = validator.validate(spec_full, phase, validator_config)

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
            # MB.1.4 — lead with the per-entity scoreboard (which entities are short), then the
            # per-finding blockers (what to add). Empty scoreboard for the non-expansion phases.
            blockers=[*scoreboard, *(f.message for f in output.findings)],
        )

    return Accepted()
