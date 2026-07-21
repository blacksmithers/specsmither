"""The single English guidance composer — :func:`compose_response` + get_planning_status.

A **minimal** guidance composer. Rather than a large family of
template-interpolated files (and a generated catalog), 0.1.0 ships one terse,
correct, English composer keyed off :class:`~specsmither.domain.enums.GuidanceVariant`.
The structured side-blocks (next-entities, recommended-moves, findings) are derived from
the same inputs the verbs already hold (the gate result + the validator output), so the
prose and the typed fields never disagree.

Two public entry points the L4 verbs call:

* :func:`compose_response` — the umbrella composer. Given the variant the verb decided on
  (``gate_passed`` / ``gate_failed`` / ``denied`` / ``phase_advance`` / ``human_handover``
  / the six get_planning_status variants / …), it produces the prose + the structured
  :class:`PlanningAgentResponse`. ``next_entities`` is capped by
  ``lifecycle_config['guidance']['maxNextEntitiesToShow']`` (default 3).
* :func:`compose_get_planning_status` — the read-only status umbrella. Picks a variant
  deterministically from session state (the get_planning_status precedence) and returns
  the composed read-only response plus the one-shot side-effects the write plan applies
  (``clear_pending_feedback`` / ``bump_last_read_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from specsmither.domain.enums import (
    GuidanceVariant,
    PlanningPhase,
    PlanningSessionStatus,
    TransitionTrigger,
)
from specsmither.lifecycle.audit import MapperContext, map_path_to_operation
from specsmither.lifecycle.gate import evaluate_phase_gate_spec_wide
from specsmither.lifecycle.guidance.field_instructions import compose_field_instructions
from specsmither.lifecycle.guidance.types import (
    FindingSummary,
    GateResult,
    NextEntity,
    Outcome,
    PlanningAgentResponse,
    RecommendedMove,
)
from specsmither.lifecycle.i18n import DEFAULT_LANGUAGE, resolve_language, t
from specsmither.lifecycle.operations_registry import OPERATIONS
from specsmither.lifecycle.state_machine import is_terminal_phase, next_phase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.gate import PhaseGateResult
    from specsmither.lifecycle.ports import SpecFull, ValidatorFinding, ValidatorOutput
    from specsmither.lifecycle.prechecks import Denied
    from specsmither.lifecycle.session_record import PlanningSessionRecord

__all__ = [
    "GetPlanningStatusComposition",
    "compose_get_planning_status",
    "compose_response",
    "pick_get_planning_status_variant",
]

#: Default cap on the expansion-phase "next to work on" list (mirrors
#: ``PLANNING_LIFECYCLE_DEFAULTS['guidance']['maxNextEntitiesToShow']``).
_DEFAULT_MAX_NEXT_ENTITIES = 3

#: Hard cap on finding-derived recommended moves (``RECOMMENDED_MOVES_CAP``).
_RECOMMENDED_MOVES_CAP = 3

#: Hard cap on the summarized findings list (``MAX_FINDINGS_PER_CATEGORY``, flattened).
_MAX_FINDINGS = 20

#: Human-readable phase names + 1-based indices (the ``PHASES`` catalog, trimmed to
#: the two fields the prose reads). ``planned`` is the sentinel (index 0).
_PHASE_META: dict[PlanningPhase, tuple[str, int]] = {
    PlanningPhase.PLANNING_SPEC: ("Spec Definition", 1),
    PlanningPhase.EPIC_DECOMPOSITION: ("Epic Decomposition", 2),
    PlanningPhase.EPIC_EXPANSION: ("Epic Expansion", 3),
    PlanningPhase.TICKET_DECOMPOSITION: ("Ticket Decomposition", 4),
    PlanningPhase.TICKET_EXPANSION: ("Ticket Expansion", 5),
    PlanningPhase.CROSS_VALIDATION: ("Cross-Validation", 6),
    PlanningPhase.PLANNED: ("Planned", 0),
}


# --------------------------------------------------------------------------- #
# Small display helpers (the resume-helpers surface, trimmed)                 #
# --------------------------------------------------------------------------- #


def _phase_human_name(phase: PlanningPhase, language: str = DEFAULT_LANGUAGE) -> str:
    """Human-readable phase name; falls back to the underscored key spelled out."""

    if phase in _PHASE_META:
        return t(language, f"phase.name.{phase.value}")
    return phase.value.replace("_", " ")


def _phase_index(phase: PlanningPhase) -> int:
    """1-based phase index (0 for the ``planned`` sentinel)."""

    meta = _PHASE_META.get(phase)
    return meta[1] if meta else 0


def _native_ops_for_phase(phase: PlanningPhase) -> list[str]:
    """Mutating operations whose native phase is ``phase``, in registry order."""

    return [
        op_def.name
        for op_def in OPERATIONS.values()
        if op_def is not None and op_def.kind == "mutating" and op_def.native_phase == phase
    ]


def _native_ops_inline(phase: PlanningPhase, language: str = DEFAULT_LANGUAGE) -> str:
    """Backtick-joined native ops for inline prose, e.g. ``\\`create_epic\\`, ...``."""

    ops = _native_ops_for_phase(phase)
    if not ops:
        return t(language, "label.none")
    return ", ".join(f"`{op}`" for op in ops)


def _max_next_entities(lifecycle_config: Mapping[str, Any] | None) -> int:
    """Read ``guidance.maxNextEntitiesToShow`` from the resolved config, with the default."""

    if lifecycle_config is None:
        return _DEFAULT_MAX_NEXT_ENTITIES
    guidance = lifecycle_config.get("guidance")
    if not isinstance(guidance, dict):
        return _DEFAULT_MAX_NEXT_ENTITIES
    value = guidance.get("maxNextEntitiesToShow")
    return value if isinstance(value, int) and value >= 0 else _DEFAULT_MAX_NEXT_ENTITIES


def _threshold_for_phase(
    phase: PlanningPhase, validator_config: Mapping[str, Any] | None
) -> float | None:
    """Per-phase gate threshold from the validator config (``thresholdForPhase``)."""

    if validator_config is None:
        return None
    thresholds = validator_config.get("thresholds")
    if not isinstance(thresholds, dict):
        return None
    if phase == PlanningPhase.PLANNING_SPEC:
        return thresholds.get("specification")
    if phase == PlanningPhase.EPIC_EXPANSION:
        return thresholds.get("epic")
    if phase == PlanningPhase.TICKET_EXPANSION:
        return thresholds.get("ticket")
    return 0


def _fmt_score(score: float | None, language: str = DEFAULT_LANGUAGE) -> str:
    """Compact score label (``not yet scored`` when unknown)."""

    if score is None:
        return t(language, "label.notScored")
    return f"{score:g}"


def _fmt_threshold(threshold: float | None, language: str = DEFAULT_LANGUAGE) -> str:
    """Compact threshold label (``n/a`` when the validator config is absent)."""

    if threshold is None:
        return t(language, "label.thresholdNa")
    return f"{threshold:g}"


# --------------------------------------------------------------------------- #
# Structured side-block builders                                              #
# --------------------------------------------------------------------------- #


def _summarize_findings(
    findings: list[ValidatorFinding], language: str = DEFAULT_LANGUAGE
) -> tuple[list[FindingSummary], str | None]:
    """Flatten validator findings into capped :class:`FindingSummary` rows + a summary.

    The summary line counts findings across distinct categories; the list is capped at
    :data:`_MAX_FINDINGS` (the rich per-category grouping is deferred to a later
    milestone).
    """

    if not findings:
        return [], None
    categories = {f.category for f in findings}
    summarized = [
        FindingSummary(
            category=str(f.category),
            message=f.message,
            severity=f.severity,
            entity_id=f.entity_id,
            path=f.path,
        )
        for f in findings[:_MAX_FINDINGS]
    ]
    summary = t(
        language,
        "findings.summary",
        {"count": len(findings), "categories": len(categories)},
    )
    return summarized, summary


def _recommended_moves(findings: list[ValidatorFinding]) -> list[RecommendedMove]:
    """Finding-derived next-step hints (``composeRecommendedMoves``).

    Sorts by ``global_impact_on_fix`` desc, then ``points_lost`` desc; maps each finding's
    ``path`` to its fix operation via the audit operation-mapper; caps at
    :data:`_RECOMMENDED_MOVES_CAP`. Findings with no path or no mapped op are skipped.
    """

    if not findings:
        return []
    ordered = sorted(
        findings,
        key=lambda f: ((f.global_impact_on_fix or 0.0), (f.points_lost or 0.0)),
        reverse=True,
    )
    moves: list[RecommendedMove] = []
    for finding in ordered:
        if len(moves) >= _RECOMMENDED_MOVES_CAP:
            break
        if not finding.path:
            continue
        operation = map_path_to_operation(
            finding.path,
            MapperContext(
                entity_type=finding.entity_type,
                severity=finding.severity,
                category=str(finding.category),
            ),
        )
        if operation is None:
            continue
        moves.append(RecommendedMove(operation=operation, rationale=finding.message))
    return moves


def _next_entities(
    gate_result: PhaseGateResult | None, cap: int
) -> list[NextEntity]:
    """Build the capped "next to work on" list from the gate's per-entity verdicts.

    Entities still ``review_needed`` come first (they are what the agent must fix next),
    then any cleared (``pass``) entities for context; the whole list is capped at ``cap``
    (the configured ``maxNextEntitiesToShow``). Binary-phase gates carry no verdicts → ``[]``.
    """

    if gate_result is None or not gate_result.entity_verdicts:
        return []
    verdicts = sorted(
        gate_result.entity_verdicts,
        key=lambda v: 0 if v.verdict == "review_needed" else 1,
    )
    entities = [
        NextEntity(
            entity_id=v.entity_id,
            entity_type=v.entity_type,
            score=v.score,
            hint=v.review_hints[0] if v.review_hints else None,
        )
        for v in verdicts
    ]
    return entities[:cap]


# --------------------------------------------------------------------------- #
# Prose bodies, keyed by variant                                              #
# --------------------------------------------------------------------------- #


def _append_field_instructions(
    body: str, phase: PlanningPhase, language: str = DEFAULT_LANGUAGE
) -> str:
    """Append the rich per-field guidance block for ``phase`` to a terse variant body.

    The phase's field catalog (shape, required/optional,
    minimum count, N/A mechanism, tier, examples) is delivered *through the prose* so the
    actor fills without guessing. 0.1.x renders the full phase catalog (no spec snapshot →
    no state detection); :func:`compose_field_instructions` supports snapshot-filtered
    rendering for a later milestone. Phases with no catalog (``planned``) add nothing.
    """

    block = compose_field_instructions(phase.value, language=language)
    if not block:
        return body
    return f"{body}\n\n{block}"


def _compose_body(
    *,
    variant: GuidanceVariant,
    phase: PlanningPhase,
    score: float | None,
    threshold: float | None,
    gate: GateResult | None,
    finding_count: int,
    next_count: int,
    denial: Denied | None,
    feedback_content: str | None,
    actions_count: int,
    next_phase_label: str,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    """Render the body for ``variant`` in ``language`` (the single-composer dispatch)."""

    idx = _phase_index(phase)
    human = _phase_human_name(phase, language)
    native = _native_ops_inline(phase, language)
    score_label = _fmt_score(score, language)
    threshold_label = _fmt_threshold(threshold, language)
    common = {"idx": idx, "human": human, "native": native, "score": score_label}

    if variant == GuidanceVariant.GATE_PASSED:
        body = t(language, "body.gatePassed", {**common, "threshold": threshold_label})
        if next_count:
            body += t(language, "body.gatePassed.nextSuffix", {"nextCount": next_count})
        # NB: the rich field catalog is NOT appended on a PASS — repeating the full ~11KB
        # per-field guidance after every successful edit induces churn (the agent re-edits an
        # already-passing entity). It rides GATE_FAILED (where the agent needs it) instead.
        return body

    if variant == GuidanceVariant.GATE_FAILED:
        body = t(
            language,
            "body.gateFailed",
            {**common, "threshold": threshold_label, "findingCount": finding_count},
        )
        return _append_field_instructions(body, phase, language)

    if variant == GuidanceVariant.DENIED:
        message = (
            denial.message
            if denial is not None
            else t(language, "body.denied.fallbackMessage")
        )
        body = t(language, "body.denied", {"message": message})
        if denial is not None and denial.blockers:
            joined = "; ".join(denial.blockers)
            body += t(language, "body.denied.blockers", {"blockers": joined})
        return body

    if variant in (GuidanceVariant.PHASE_ADVANCE, GuidanceVariant.PHASE_ADVANCED_AFTER_APPROVE):
        # Entering a phase: render the per-field catalog (fields to fill + threshold + native
        # ops), mirroring composePhaseIntro — the agent needs it before its first op, not only
        # after a gate failure.
        body = t(language, "body.phaseAdvance", common)
        return _append_field_instructions(body, phase, language)

    if variant == GuidanceVariant.PHASE_ROLLBACK:
        return t(language, "body.phaseRollback", common)

    if variant in (GuidanceVariant.HUMAN_HANDOVER, GuidanceVariant.PHASE_COMPLETE):
        return t(language, "body.humanHandover")

    if variant == GuidanceVariant.AWAITING_HUMAN_REVIEW_HANDOVER:
        return t(language, "body.awaitingHumanReview", {"nextPhaseLabel": next_phase_label})

    if variant in (GuidanceVariant.HUMAN_FEEDBACK, GuidanceVariant.HUMAN_FEEDBACK_RECEIVED):
        quoted = (
            t(language, "body.humanFeedback.quoted", {"content": feedback_content})
            if feedback_content
            else ""
        )
        return t(language, "body.humanFeedback", {**common, "quoted": quoted})

    if variant == GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK:
        return t(language, "body.humanRejected", common)

    if variant == GuidanceVariant.SESSION_CLOSED:
        return t(
            language,
            "body.sessionClosed",
            {"human": human, "score": score_label, "actionsCount": actions_count},
        )

    # GuidanceVariant.PHASE_STATUS_REPORT (and any unmatched variant).
    gate_label = gate or t(language, "label.gateUnknown")
    if gate == "pass":
        hint = t(language, "body.statusReport.hintPass")
    elif gate == "fail":
        hint = t(language, "body.statusReport.hintFail")
    else:
        hint = t(language, "body.statusReport.hintUnknown")
    # PHASE_STATUS_REPORT is the cold-start / orientation body (SPS create, inspect): render the
    # per-field catalog so a freshly-started session gets the fill-in guidance up front.
    body = t(
        language,
        "body.statusReport",
        {**common, "threshold": threshold_label, "gate": gate_label, "hint": hint},
    )
    return _append_field_instructions(body, phase, language)


# --------------------------------------------------------------------------- #
# The umbrella composer                                                       #
# --------------------------------------------------------------------------- #


#: Sentinel for the ``gate_outcome`` override — distinguishes "not provided" (derive the
#: gate) from an explicit ``None`` ("unknown", echo it verbatim).
_UNSET: Any = object()


def compose_response(
    *,
    variant: GuidanceVariant,
    session: PlanningSessionRecord,
    spec_full: SpecFull | None = None,
    validator_output: ValidatorOutput | None = None,
    gate_result: PhaseGateResult | None = None,
    denial: Denied | None = None,
    lifecycle_config: Mapping[str, Any] | None = None,
    validator_config: Mapping[str, Any] | None = None,
    score_global: float | None = None,
    gate_outcome: str | None | Any = _UNSET,
) -> PlanningAgentResponse:
    """Compose the :class:`PlanningAgentResponse` a verb returns, keyed by ``variant``.

    ``session`` supplies the post-mutation state echoed back (phase / status / cached
    score+gate). ``validator_output`` (when the verb re-validated) supplies the fresh
    score, gate verdict, and findings; otherwise the session's cached values are shown.
    ``gate_result`` supplies the per-entity verdicts the ``next_entities`` block is built
    from. ``denial`` carries the denial prose for the ``denied`` variant. ``next_entities``
    is capped by ``lifecycle_config['guidance']['maxNextEntitiesToShow']`` (default 3);
    ``recommended_moves`` and ``findings`` are derived from the validator findings.
    """

    language = resolve_language(lifecycle_config)
    phase = PlanningPhase(session.current_phase)
    status = PlanningSessionStatus(session.status)
    spec_id = spec_full.spec.id if spec_full is not None else session.specification_id

    score = validator_output.local_score if validator_output is not None else session.last_score
    if gate_outcome is not _UNSET:
        # An explicit per-branch verdict from the caller (CPS hard-codes it per outcome;
        # inspect echoes the persisted ``last_gate_result``). Highest precedence — never
        # re-derived, so the reported ``gate_result`` cannot contradict the verb outcome.
        gate: GateResult | None = _coerce_gate(gate_outcome)
    elif gate_result is not None:
        gate = gate_result.gate_outcome
    elif validator_output is not None:
        # Report the PHASE-GATE verdict, not the validator's composite ``gate_result``:
        # at the ``*_expansion`` phases the composite folds in the global cascade
        # (topology/DAG), which is NOT part of the phase-completion gate — surfacing it
        # here produced a self-contradiction ("gate failing, score 100 / threshold 70").
        # Recompute the spec-wide phase gate so the status report agrees with what
        # ``complete_planning_session`` will actually decide. Falls back to the composite
        # when the spec could not be loaded (no per-entity ids to evaluate).
        if spec_full is not None and validator_config is not None:
            gate = evaluate_phase_gate_spec_wide(
                current_phase=phase,
                validator_output=validator_output,
                spec_full=spec_full,
                validator_config=validator_config,
            ).gate_outcome
        else:
            gate = validator_output.gate_result
    else:
        gate = _coerce_gate(session.last_gate_result)

    findings = list(validator_output.findings) if validator_output is not None else []
    summarized, findings_summary = _summarize_findings(findings, language)
    moves = _recommended_moves(findings)
    next_entities = _next_entities(gate_result, _max_next_entities(lifecycle_config))

    feedback = session.pending_human_feedback
    feedback_content = (
        feedback.get("content") if isinstance(feedback, dict) else None
    )

    next_phase_label = _next_phase_label(phase, language)

    body = _compose_body(
        variant=variant,
        phase=phase,
        score=score,
        threshold=_threshold_for_phase(phase, validator_config),
        gate=gate,
        finding_count=len(findings),
        next_count=len(next_entities),
        denial=denial,
        feedback_content=feedback_content,
        actions_count=session.actions_count or 0,
        next_phase_label=next_phase_label,
        language=language,
    )

    outcome: Outcome = "denied" if variant == GuidanceVariant.DENIED else "success"

    return PlanningAgentResponse(
        outcome=outcome,
        session_id=session.id,
        spec_id=spec_id,
        phase=phase,
        status=status,
        variant=variant,
        guidance=body,
        gate_result=gate,
        score=score,
        score_global=score_global,
        next_entities=next_entities,
        recommended_moves=moves,
        findings=summarized,
        findings_summary=findings_summary,
    )


def _coerce_gate(value: str | None) -> GateResult | None:
    """Narrow a cached ``last_gate_result`` string to the typed :data:`GateResult`."""

    if value == "pass":
        return "pass"
    if value == "fail":
        return "fail"
    return None


def _next_phase_label(phase: PlanningPhase, language: str = DEFAULT_LANGUAGE) -> str:
    """The 'on approval, you advance to ...' label (the terminal phase closes the spec)."""

    if is_terminal_phase(phase):
        return t(language, "label.finalized")
    nxt = next_phase(phase)
    if nxt is None:
        return t(language, "label.finalized")
    return t(language, "label.advancesTo", {"phase": _phase_human_name(nxt, language)})


# --------------------------------------------------------------------------- #
# get_planning_status — variant pick + read-only composition                  #
# --------------------------------------------------------------------------- #


def pick_get_planning_status_variant(session: PlanningSessionRecord) -> GuidanceVariant:
    """Deterministically pick the get_planning_status variant from session state.

    Pure function of the session (``pickGetPlanningStatusVariant``). Precedence:
    ``closed`` → ``session_closed``; ``awaiting_human_review`` →
    ``awaiting_human_review_handover``; active with pending feedback →
    ``human_feedback_received``; active with an *unread* transition
    (``last_read_at < last_transition_at``) triggered by ``human_approve`` →
    ``phase_advanced_after_approve``; by ``human_reject_no_feedback`` →
    ``human_rejected_no_feedback``; otherwise ``phase_status_report``.
    """

    if session.status == PlanningSessionStatus.CLOSED.value:
        return GuidanceVariant.SESSION_CLOSED
    if session.status == PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value:
        return GuidanceVariant.AWAITING_HUMAN_REVIEW_HANDOVER

    # session.status == 'active'
    if session.pending_human_feedback:
        return GuidanceVariant.HUMAN_FEEDBACK_RECEIVED

    if _is_unread_transition(session):
        if session.last_transition_trigger == TransitionTrigger.HUMAN_APPROVE.value:
            return GuidanceVariant.PHASE_ADVANCED_AFTER_APPROVE
        if session.last_transition_trigger == TransitionTrigger.HUMAN_REJECT_NO_FEEDBACK.value:
            return GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK

    return GuidanceVariant.PHASE_STATUS_REPORT


def _is_unread_transition(session: PlanningSessionRecord) -> bool:
    """Whether the latest transition has not yet been announced (``lastReadAt < lastTransitionAt``).

    Timestamps are ISO-8601 strings (lexicographically ordered for a fixed format). A
    missing ``last_transition_at`` means no transition to announce → ``False``; a missing
    ``last_read_at`` means never read → the transition is unread → ``True``.
    """

    transition_at = session.last_transition_at
    if transition_at is None:
        return False
    read_at = session.last_read_at
    if read_at is None:
        return True
    return read_at < transition_at


@dataclass(frozen=True)
class GetPlanningStatusComposition:
    """The read-only get_planning_status result + its one-shot write-plan side-effects.

    ``response`` is the composed :class:`PlanningAgentResponse`; ``clear_pending_feedback``
    instructs the write plan to null ``pending_human_feedback`` atomically with the audit
    append (one-shot feedback delivery); ``bump_last_read_at`` instructs it to bump
    ``last_read_at`` so a transition announcement is not repeated.
    """

    variant: GuidanceVariant
    response: PlanningAgentResponse
    clear_pending_feedback: bool
    bump_last_read_at: bool


def compose_get_planning_status(
    session: PlanningSessionRecord,
    *,
    lifecycle_config: Mapping[str, Any] | None = None,
    validator_config: Mapping[str, Any] | None = None,
) -> GetPlanningStatusComposition:
    """Compose the read-only get_planning_status response (``composeGetPlanningStatus``).

    Picks the variant via :func:`pick_get_planning_status_variant`, composes the read-only
    response from the session's cached state (never re-runs the validator), and flags the
    one-shot side-effects: ``human_feedback_received`` clears the pending feedback;
    ``phase_advanced_after_approve`` / ``human_rejected_no_feedback`` bump ``last_read_at``.
    """

    variant = pick_get_planning_status_variant(session)
    clear_pending_feedback = variant == GuidanceVariant.HUMAN_FEEDBACK_RECEIVED
    bump_last_read_at = variant in (
        GuidanceVariant.PHASE_ADVANCED_AFTER_APPROVE,
        GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK,
    )
    response = compose_response(
        variant=variant,
        session=session,
        lifecycle_config=lifecycle_config,
        validator_config=validator_config,
    )
    return GetPlanningStatusComposition(
        variant=variant,
        response=response,
        clear_pending_feedback=clear_pending_feedback,
        bump_last_read_at=bump_last_read_at,
    )
