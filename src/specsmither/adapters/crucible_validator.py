"""Crucible-backed :class:`~specsmither.lifecycle.ports.Validator` (work item #5).

Faithful port of ``lifecycle/adapters/validator-adapter.ts`` (A1 §3.2). This is
the one *coupled* seam between the pure planning lifecycle and crucible — the
deterministic OpenSpec validation engine. The lifecycle only ever reads the flat
:class:`~specsmither.lifecycle.ports.ValidatorOutput` contract; this adapter maps
crucible's richer four-layer result down to it.

The mapping in one paragraph
----------------------------

:meth:`CrucibleValidatorAdapter.validate` short-circuits the terminal ``planned``
sentinel (and any non-validator phase) to a trivial pass *before* touching
crucible. For the six real phases it dumps the nested
:class:`~crucible.models.Specification` to crucible's camelCase wire dict, scopes
``activeEntityId`` (the two ``*_expansion`` phases score per entity, so they pass
the full id list; everything else — including ``ticket_decomposition``, which is
spec-wide — passes ``None``), and calls :func:`crucible.validate` for the
``structural`` / ``scoring`` / ``guidance`` layers. ``gate_result`` / ``local_score``
come from the active ``scoring`` layer (a *skipped* or *absent* scoring layer maps
to ``'fail'`` / ``0.0``, mirroring the TS ``scoring && !scoring.skipped`` guard);
the per-entity scores route to ``per_epic_score`` on ``epic_expansion`` and
``per_ticket_score`` on ``ticket_expansion`` (``{}`` on every other phase); and
``findings`` flattens crucible's structural findings + per-entity guidance entries
into the lifecycle's :class:`~specsmither.lifecycle.ports.ValidatorFinding` list.

Faithful-port notes
--------------------

* crucible's ``validate`` is **synchronous** (the engine has no async I/O) — the
  TS ``Promise`` wrapper drops, exactly as the rest of the SpecSmither seam went
  sync.
* :data:`_VALIDATOR_OP_TO_PATH` keys are crucible's ``OperationName`` vocabulary
  (``add_dependencies`` / ``remove_dependency`` / ``link_blueprint`` …), *not* the
  lifecycle's 15-op planning names — cross-validation guidance entries carry the
  validator-side op names, so the table is ported verbatim from the TS adapter.
* Every emitted finding carries ``severity='finding'`` (advisory); crucible's own
  ``error``/``warning`` severities are not surfaced — the TS adapter hardcodes
  ``'finding'`` because the gate, not the finding, is the hard signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from crucible import validate as crucible_validate
from crucible.types import (
    GuidanceCrossValidationEntry,
    GuidanceMessage,
    ScoringResultActive,
    StructuralFinding,
    ValidationResult,
)

from specsmither.domain.enums import FindingCategory, PlanningPhase
from specsmither.lifecycle.ports import SpecFull, ValidatorFinding, ValidatorOutput

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["CrucibleValidatorAdapter"]


# --------------------------------------------------------------------------- #
# Phase set + op→path table (ported verbatim from validator-adapter.ts)        #
# --------------------------------------------------------------------------- #

#: Phases crucible recognises — the six real phases, excluding the ``planned``
#: terminal sentinel the validator does not know about.
_VALIDATOR_PHASES: frozenset[str] = frozenset(
    {
        PlanningPhase.PLANNING_SPEC,
        PlanningPhase.EPIC_DECOMPOSITION,
        PlanningPhase.EPIC_EXPANSION,
        PlanningPhase.TICKET_DECOMPOSITION,
        PlanningPhase.TICKET_EXPANSION,
        PlanningPhase.CROSS_VALIDATION,
    }
)

#: crucible validator op name → lifecycle cross-validation path prefix. Keys are
#: crucible's ``OperationName`` vocabulary, surfaced on cross-validation guidance
#: entries' ``operations``.
_VALIDATOR_OP_TO_PATH: dict[str, str] = {
    "set_metadata": "/cross-validation/by-op/set_metadata",
    "create_epic": "/cross-validation/by-op/create_epic",
    "update_epic": "/cross-validation/by-op/update_epic",
    "delete_epic": "/cross-validation/by-op/delete_epic",
    "create_ticket": "/cross-validation/by-op/create_ticket",
    "update_ticket": "/cross-validation/by-op/update_ticket",
    "delete_ticket": "/cross-validation/by-op/delete_ticket",
    "add_dependencies": "/cross-validation/by-op/add_dependencies",
    "remove_dependency": "/cross-validation/by-op/remove_dependency",
    "create_blueprint": "/cross-validation/by-op/create_blueprint",
    "update_blueprint": "/cross-validation/by-op/update_blueprint",
    "delete_blueprint": "/cross-validation/by-op/delete_blueprint",
    "link_blueprint": "/cross-validation/by-op/link_blueprint",
    "unlink_blueprint": "/cross-validation/by-op/unlink_blueprint",
}


# --------------------------------------------------------------------------- #
# Pure mapping helpers                                                         #
# --------------------------------------------------------------------------- #


def _entity_path(entity_type: str, entity_id: str) -> str:
    """``entityType``/``entityId`` → the lifecycle locator path (``entityPath``)."""
    if entity_type == "specification":
        return "/spec"
    if entity_type == "epic":
        return f"/epics/{entity_id}"
    return f"/tickets/{entity_id}"  # ticket


def _structural_finding_to_finding(finding: StructuralFinding) -> ValidatorFinding:
    """Map a crucible structural finding → a ``schema`` / ``count`` finding."""
    category = finding.category
    is_count = "count" in category or category == "entity-count"
    return ValidatorFinding(
        category=FindingCategory.COUNT if is_count else FindingCategory.SCHEMA,
        message=finding.message,
        severity="finding",
        entity_id=None,
        entity_type=None,
        path=f"/structural/{finding.field}",
        points_lost=0.0,
        global_impact_on_fix=0.0,
    )


def _guidance_message_to_finding(entry: GuidanceMessage) -> ValidatorFinding:
    """Map a crucible per-entity guidance message → a ``rubric`` finding."""
    return ValidatorFinding(
        category=FindingCategory.RUBRIC,
        message=entry.message,
        severity="finding",
        entity_id=entry.entity_id,
        entity_type=entry.entity_type,
        path=_entity_path(entry.entity_type, entry.entity_id),
        points_lost=entry.points_lost,
        global_impact_on_fix=entry.global_impact_on_fix,
    )


def _cross_validation_entry_to_finding(
    entry: GuidanceCrossValidationEntry,
) -> ValidatorFinding:
    """Map a crucible cross-validation entry → a ``cross-validation`` finding."""
    first_op = entry.operations[0] if entry.operations else None
    if first_op is not None:
        path = _VALIDATOR_OP_TO_PATH.get(first_op, f"/cross-validation/{first_op}")
    else:
        path = "/cross-validation/unknown"
    return ValidatorFinding(
        category=FindingCategory.CROSS_VALIDATION,
        message=entry.message,
        severity="finding",
        entity_id=entry.primary_entity_id,
        entity_type=None,
        path=path,
        points_lost=0.0,
        global_impact_on_fix=0.0,
    )


def _collect_findings(result: ValidationResult) -> list[ValidatorFinding]:
    """Flatten structural findings + per-entity guidance into the flat list."""
    findings: list[ValidatorFinding] = []

    # Structural findings (schema / count checks).
    if result.structural is not None:
        for structural in result.structural.findings:
            findings.append(_structural_finding_to_finding(structural))

    # Guidance findings (rubric / cross-validation), keyed per entity.
    if result.guidance is not None:
        for entries in result.guidance.per_entity.values():
            for entry in entries:
                if isinstance(entry, GuidanceCrossValidationEntry):
                    findings.append(_cross_validation_entry_to_finding(entry))
                else:
                    findings.append(_guidance_message_to_finding(entry))

    return findings


def _get_active_entity_id(
    spec_full: SpecFull, phase: PlanningPhase
) -> str | list[str] | None:
    """Scope crucible's ``activeEntityId`` (``getActiveEntityId``).

    Only the per-entity SCORE phases carry an active entity: ``epic_expansion``
    scopes to the epic ids being scored, ``ticket_expansion`` to the ticket ids
    (flattened across epics). ``ticket_decomposition`` is spec-wide (its ratio +
    structural checks span ALL tickets of ALL epics), so — like every other phase
    — it returns ``None``, which crucible resolves to the whole spec.
    """
    if phase == PlanningPhase.EPIC_EXPANSION:
        return [epic.id for epic in spec_full.epics]
    if phase == PlanningPhase.TICKET_EXPANSION:
        return [ticket.id for epic in spec_full.epics for ticket in epic.tickets]
    return None


# --------------------------------------------------------------------------- #
# The adapter                                                                  #
# --------------------------------------------------------------------------- #


class CrucibleValidatorAdapter:
    """Implements :class:`~specsmither.lifecycle.ports.Validator` over crucible.

    Stateless — a single instance is safe to share across the lifecycle. The
    ``config`` argument is the already-merged effective ``ValidatorConfig`` dict
    (defaults + project overrides + frozen spec snapshot); this adapter passes it
    straight through to :func:`crucible.validate`.
    """

    def validate(
        self, spec_full: SpecFull, phase: PlanningPhase, config: Mapping[str, Any]
    ) -> ValidatorOutput:
        """Score ``spec_full`` for ``phase`` → a flat :class:`ValidatorOutput`."""
        # 'planned' (and any non-validator phase) → trivial pass; never call
        # crucible, which does not recognise the terminal sentinel.
        if phase not in _VALIDATOR_PHASES:
            return ValidatorOutput(
                gate_result="pass",
                local_score=1.0,
                per_epic_score={},
                per_ticket_score={},
                findings=[],
                validated_phase=phase,
            )

        active_entity_id = _get_active_entity_id(spec_full, phase)
        spec_dict = spec_full.spec.model_dump(by_alias=True, exclude_none=True)

        context: dict[str, Any] = {
            "phase": phase,
            "config": config,
            "activeEntityId": active_entity_id,
            "returns": ["structural", "scoring", "guidance"],
        }
        result = cast(ValidationResult, crucible_validate(spec_dict, context))

        # gate_result is the AUTHORITATIVE per-phase verdict crucible composes
        # across all layers (structural counts for the decomposition phases,
        # the cross-validation layer for cross_validation, scoring-threshold for
        # the rubric phases) — exposed as the top-level ``result.passed``. Reading
        # only ``scoring.gate_result`` (as the TS adapter did) wrongly fails every
        # non-scoring phase, making the loop unable to reach ``ready``.
        # local_score / per-entity scores still come from the active scoring layer
        # (a skipped/absent layer has no rubric score → 0.0).
        scoring = result.scoring
        gate_result: Literal["pass", "fail"] = "pass" if result.passed else "fail"
        if isinstance(scoring, ScoringResultActive):
            local_score = scoring.local_score
            per_entity_score = scoring.per_entity_score or {}
        else:
            local_score = 0.0
            per_entity_score = {}

        # epic_expansion populates epic ids; ticket_expansion populates ticket
        # ids; every other phase leaves both empty (the validator only scores
        # per-entity on the two expansion phases).
        per_epic_score = per_entity_score if phase == PlanningPhase.EPIC_EXPANSION else {}
        per_ticket_score = (
            per_entity_score if phase == PlanningPhase.TICKET_EXPANSION else {}
        )

        return ValidatorOutput(
            gate_result=gate_result,
            local_score=local_score,
            per_epic_score=per_epic_score,
            per_ticket_score=per_ticket_score,
            findings=_collect_findings(result),
            validated_phase=phase,
        )
