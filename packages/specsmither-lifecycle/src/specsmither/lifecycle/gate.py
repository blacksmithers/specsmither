"""The phase gate evaluator — the keystone pure composition.

The gate is
deliberately **thin**: it does not itself recompute cascade / structural / N-A
rubric logic — all of that lives inside the validator (crucible). For the binary
phases (``planning_spec`` / the two ``*_decomposition`` phases / ``cross_validation``)
the gate simply *trusts* :attr:`ValidatorOutput.gate_result`, which has already
folded ``score >= threshold`` **and** the cascade gate. For the two
``*_expansion`` phases the gate does a pure per-entity threshold comparison
(``per_epic_score[id] >= thresholds['epic']`` / ``per_ticket_score[id] >=
thresholds['ticket']``) — a passing phase requires **every** touched entity to
clear its threshold; an empty touched-entity set fails.

Beyond the binary verdict the gate emits two side products:

* :class:`EntityVerdict` per touched expansion entity — its ``verdict``
  (``pass`` / ``review_needed``) and the ``review_hints`` grouped from the
  validator findings — consumed by the L4 guidance composer.
* :class:`EntityScoreWrite` rows — the score-datapoint writes the L3 WritePlan
  builders persist. Each carries a :class:`~specsmither.domain.enums.DatapointTrigger`
  baked in per phase (``metadata_updated`` / ``epic_field_updated`` /
  ``ticket_field_updated`` / ``cross_val_recompute``). ``cross_validation``
  emits a *full refresh*: the spec plus **every** epic and ticket.

A separate ``sessionLastGateResult`` field is not carried — it would only ever
mirror the gate outcome; the L3 session write reads
:attr:`PhaseGateResult.gate_outcome` directly.

``touched_entity_ids`` is a flat collection: it holds the touched epic ids in
``epic_expansion``, the touched ticket ids in ``ticket_expansion``, and the spec
id in ``planning_spec`` / ``cross_validation`` (the spec-level write target). The
``*_decomposition`` and ``planned`` phases ignore it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from specsmither.domain.enums import DatapointTrigger, PlanningPhase
from specsmither.lifecycle.ports import SpecFull, ValidatorFinding, ValidatorOutput

__all__ = [
    "EntityScoreWrite",
    "EntityVerdict",
    "PhaseGateResult",
    "evaluate_phase_gate",
    "evaluate_phase_gate_spec_wide",
    "read_gate_thresholds",
]


# --------------------------------------------------------------------------- #
# Result data contracts                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntityVerdict:
    """Per-entity verdict for an expansion-phase gate (``EntityVerdict``).

    Only the two ``*_expansion`` phases populate verdicts; the binary phases
    return ``[]``. ``verdict`` is ``pass`` when the entity's
    score cleared its phase threshold, else ``review_needed``. ``review_hints``
    are the validator findings' messages grouped by ``entity_id`` — the L4
    guidance composer renders them under each entity.
    """

    entity_id: str
    entity_type: Literal["epic", "ticket"]
    score: float
    verdict: Literal["pass", "review_needed"]
    review_hints: list[str]


@dataclass(frozen=True)
class EntityScoreWrite:
    """A score-datapoint write the WritePlan builders persist (``ScoreWriteEntry``).

    ``trigger`` is a :class:`~specsmither.domain.enums.DatapointTrigger` baked in
    per phase (``metadata_updated`` / ``epic_field_updated`` /
    ``ticket_field_updated`` / ``cross_val_recompute``). L3 fuses the write into
    the matching ``specMutation`` row when the mutation target equals the score
    target, and always appends a separate score datapoint.
    """

    entity_type: Literal["spec", "epic", "ticket"]
    entity_id: str
    score: float
    trigger: DatapointTrigger


@dataclass(frozen=True)
class PhaseGateResult:
    """The gate verdict + its side products (``PhaseGateResult``).

    ``gate_outcome`` is the binary lifecycle verdict; ``entity_verdicts`` is
    populated only for the expansion phases; ``rationale`` is human-readable
    prose for guidance/audit; ``entity_score_writes`` are the datapoint writes
    L3 persists. (There is no separate ``sessionLastGateResult`` mirror — read
    ``gate_outcome`` directly.)
    """

    gate_outcome: Literal["pass", "fail"]
    entity_verdicts: list[EntityVerdict] = field(default_factory=list)
    rationale: str = ""
    entity_score_writes: list[EntityScoreWrite] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _ordered(ids: Iterable[str] | None) -> list[str]:
    """Normalise an id collection to a deterministic list.

    A ``set`` / ``frozenset`` is sorted (set iteration order is not stable across
    processes); a list/tuple keeps the caller's explicit order (which carries
    meaning). ``None`` collapses to ``[]``.
    """

    if ids is None:
        return []
    if isinstance(ids, set | frozenset):
        return sorted(ids)
    return list(ids)


def _finding_hints_for_entity(findings: list[ValidatorFinding], entity_id: str) -> list[str]:
    """Group finding messages for ``entity_id`` (``findingHintsForEntity``, lines 45-49)."""

    return [f.message for f in findings if f.entity_id == entity_id]


# --------------------------------------------------------------------------- #
# The gate                                                                     #
# --------------------------------------------------------------------------- #


def evaluate_phase_gate(
    *,
    current_phase: PlanningPhase,
    validator_output: ValidatorOutput,
    touched_entity_ids: set[str] | list[str],
    thresholds: Mapping[str, float],
    all_epic_ids: Iterable[str] | None = None,
    all_ticket_ids: Iterable[str] | None = None,
) -> PhaseGateResult:
    """Evaluate the phase gate for ``current_phase`` (``evaluatePhaseGate``, lines 51-246).

    A pure switch on ``current_phase``:

    * ``planning_spec`` — ``gate_outcome = validator_output.gate_result``; emit
      one spec ``metadata_updated`` score write (local score).
    * ``epic_decomposition`` / ``ticket_decomposition`` — binary
      ``gate_result``; no score writes.
    * ``epic_expansion`` — pass iff **every** touched epic
      ``per_epic_score[id] >= thresholds['epic']``; per-epic verdicts +
      ``epic_field_updated`` score writes. Empty touched set → ``fail``.
    * ``ticket_expansion`` — symmetric over tickets / ``thresholds['ticket']`` /
      ``ticket_field_updated``.
    * ``cross_validation`` — binary ``gate_result`` **plus** a full refresh:
      ``cross_val_recompute`` score writes for the spec + every epic
      (``all_epic_ids``) + every ticket (``all_ticket_ids``).
    * ``planned`` — terminal marker; trivially ``pass`` with no writes.
    """

    findings = validator_output.findings

    match current_phase:
        case PlanningPhase.PLANNING_SPEC:
            spec_id = next(iter(_ordered(touched_entity_ids)), "")
            local_score = validator_output.local_score
            # The lifecycle verdict equals the validator's own gate_result, which
            # already folds BOTH `local_score >= threshold` AND the cascade gate.
            gate_outcome: Literal["pass", "fail"] = validator_output.gate_result
            score_writes = (
                [
                    EntityScoreWrite(
                        entity_type="spec",
                        entity_id=spec_id,
                        score=local_score,
                        trigger=DatapointTrigger.METADATA_UPDATED,
                    )
                ]
                if spec_id
                else []
            )
            return PhaseGateResult(
                gate_outcome=gate_outcome,
                entity_verdicts=[],
                rationale=(
                    f"planning_spec gate: spec_score={local_score} "
                    f"threshold={thresholds['specification']} "
                    f"validator_gate={validator_output.gate_result}"
                ),
                entity_score_writes=score_writes,
            )

        case PlanningPhase.EPIC_DECOMPOSITION | PlanningPhase.TICKET_DECOMPOSITION:
            # Binary gate — no entity score writes.
            return PhaseGateResult(
                gate_outcome=validator_output.gate_result,
                entity_verdicts=[],
                rationale=(
                    f"{current_phase.value} gate: "
                    f"binary validator result={validator_output.gate_result}"
                ),
                entity_score_writes=[],
            )

        case PlanningPhase.EPIC_EXPANSION:
            epic_ids = _ordered(touched_entity_ids)
            if not epic_ids:
                return PhaseGateResult(
                    gate_outcome="fail",
                    entity_verdicts=[],
                    rationale="epic_expansion gate: no touched epics provided",
                    entity_score_writes=[],
                )
            threshold = thresholds["epic"]
            verdicts = [
                EntityVerdict(
                    entity_id=entity_id,
                    entity_type="epic",
                    score=(score := validator_output.per_epic_score.get(entity_id, 0.0)),
                    verdict="pass" if score >= threshold else "review_needed",
                    review_hints=_finding_hints_for_entity(findings, entity_id),
                )
                for entity_id in epic_ids
            ]
            passed = sum(1 for v in verdicts if v.verdict == "pass")
            outcome: Literal["pass", "fail"] = "pass" if passed == len(verdicts) else "fail"
            score_writes = [
                EntityScoreWrite(
                    entity_type="epic",
                    entity_id=entity_id,
                    score=validator_output.per_epic_score.get(entity_id, 0.0),
                    trigger=DatapointTrigger.EPIC_FIELD_UPDATED,
                )
                for entity_id in epic_ids
            ]
            return PhaseGateResult(
                gate_outcome=outcome,
                entity_verdicts=verdicts,
                rationale=(
                    f"epic_expansion gate: {passed}/{len(epic_ids)} "
                    f"epics passed threshold={threshold}"
                ),
                entity_score_writes=score_writes,
            )

        case PlanningPhase.TICKET_EXPANSION:
            ticket_ids = _ordered(touched_entity_ids)
            if not ticket_ids:
                return PhaseGateResult(
                    gate_outcome="fail",
                    entity_verdicts=[],
                    rationale="ticket_expansion gate: no touched tickets provided",
                    entity_score_writes=[],
                )
            threshold = thresholds["ticket"]
            verdicts = [
                EntityVerdict(
                    entity_id=entity_id,
                    entity_type="ticket",
                    score=(score := validator_output.per_ticket_score.get(entity_id, 0.0)),
                    verdict="pass" if score >= threshold else "review_needed",
                    review_hints=_finding_hints_for_entity(findings, entity_id),
                )
                for entity_id in ticket_ids
            ]
            passed = sum(1 for v in verdicts if v.verdict == "pass")
            outcome = "pass" if passed == len(verdicts) else "fail"
            score_writes = [
                EntityScoreWrite(
                    entity_type="ticket",
                    entity_id=entity_id,
                    score=validator_output.per_ticket_score.get(entity_id, 0.0),
                    trigger=DatapointTrigger.TICKET_FIELD_UPDATED,
                )
                for entity_id in ticket_ids
            ]
            return PhaseGateResult(
                gate_outcome=outcome,
                entity_verdicts=verdicts,
                rationale=(
                    f"ticket_expansion gate: {passed}/{len(ticket_ids)} "
                    f"tickets passed threshold={threshold}"
                ),
                entity_score_writes=score_writes,
            )

        case PlanningPhase.CROSS_VALIDATION:
            # Binary gate + full refresh of every entity in the spec.
            spec_id = next(iter(_ordered(touched_entity_ids)), "")
            epics = _ordered(all_epic_ids)
            tickets = _ordered(all_ticket_ids)
            full_refresh: list[EntityScoreWrite] = []
            if spec_id:
                full_refresh.append(
                    EntityScoreWrite(
                        entity_type="spec",
                        entity_id=spec_id,
                        score=validator_output.local_score,
                        trigger=DatapointTrigger.CROSS_VAL_RECOMPUTE,
                    )
                )
            full_refresh.extend(
                EntityScoreWrite(
                    entity_type="epic",
                    entity_id=entity_id,
                    score=validator_output.per_epic_score.get(entity_id, 0.0),
                    trigger=DatapointTrigger.CROSS_VAL_RECOMPUTE,
                )
                for entity_id in epics
            )
            full_refresh.extend(
                EntityScoreWrite(
                    entity_type="ticket",
                    entity_id=entity_id,
                    score=validator_output.per_ticket_score.get(entity_id, 0.0),
                    trigger=DatapointTrigger.CROSS_VAL_RECOMPUTE,
                )
                for entity_id in tickets
            )
            return PhaseGateResult(
                gate_outcome=validator_output.gate_result,
                entity_verdicts=[],
                rationale=(
                    f"cross_validation gate: binary validator result="
                    f"{validator_output.gate_result}, full refresh of "
                    f"{len(epics)} epics + {len(tickets)} tickets"
                ),
                entity_score_writes=full_refresh,
            )

        case PlanningPhase.PLANNED:
            # Terminal marker phase — treat as a trivial binary pass.
            return PhaseGateResult(
                gate_outcome="pass",
                entity_verdicts=[],
                rationale="planned is a terminal marker phase; gate trivially passes",
                entity_score_writes=[],
            )

        case _:  # pragma: no cover — exhaustive over PlanningPhase.
            return PhaseGateResult(
                gate_outcome="fail",
                entity_verdicts=[],
                rationale=f"Unknown phase: {current_phase}",
                entity_score_writes=[],
            )


# --------------------------------------------------------------------------- #
# Spec-wide convenience — the gate as the CPS check + the guidance composer     #
# evaluate it: EVERY entity of the phase's type, so the `*_expansion` verdict   #
# is the spec-wide all-pass (not a touched subset). One home so the CPS gate    #
# and the status-report guidance always report the SAME verdict (the read-only  #
# `get_planning_status` poll re-validates live and routes through here too).    #
# --------------------------------------------------------------------------- #


def read_gate_thresholds(validator_config: Mapping[str, Any]) -> dict[str, float]:
    """Read the ``{specification, epic, ticket}`` gate thresholds from the config.

    Missing / non-numeric entries collapse to ``0.0`` (a permissive gate rather
    than a crash) — the same shape ``evaluate_phase_gate`` expects.
    """

    raw = validator_config.get("thresholds")
    raw = raw if isinstance(raw, Mapping) else {}

    def _f(key: str) -> float:
        try:
            return float(raw.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    return {"specification": _f("specification"), "epic": _f("epic"), "ticket": _f("ticket")}


def evaluate_phase_gate_spec_wide(
    *,
    current_phase: PlanningPhase,
    validator_output: ValidatorOutput,
    spec_full: SpecFull,
    validator_config: Mapping[str, Any],
) -> PhaseGateResult:
    """:func:`evaluate_phase_gate` with the SPEC-WIDE all-pass scope.

    The two ``*_expansion`` phases gate on **every** epic/ticket in the spec (the
    per-entity all-pass — NOT a touched subset, and deliberately WITHOUT the global
    cascade, which binds only at ``cross_validation``); the spec-level phases carry
    the spec id. Both the CPS gate (``gate_currently_passing``) and the guidance
    composer call this so they report the SAME verdict the phase actually enforces.
    """

    all_epic_ids = [epic.id for epic in spec_full.epics]
    all_ticket_ids = [ticket.id for epic in spec_full.epics for ticket in epic.tickets]
    if current_phase == PlanningPhase.EPIC_EXPANSION:
        touched: list[str] = all_epic_ids
    elif current_phase == PlanningPhase.TICKET_EXPANSION:
        touched = all_ticket_ids
    else:
        touched = [spec_full.spec.id]

    return evaluate_phase_gate(
        current_phase=current_phase,
        validator_output=validator_output,
        touched_entity_ids=touched,
        thresholds=read_gate_thresholds(validator_config),
        all_epic_ids=all_epic_ids,
        all_ticket_ids=all_ticket_ids,
    )
