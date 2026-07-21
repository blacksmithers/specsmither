"""Crucible-backed :class:`~specsmither.lifecycle.ports.Validator`.

The one *coupled* seam between the pure planning lifecycle and crucible — the
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
to ``'fail'`` / ``0.0``); the per-entity scores route to ``per_epic_score`` on
``epic_expansion`` and ``per_ticket_score`` on ``ticket_expansion`` (``{}`` on
every other phase); and ``findings`` flattens crucible's structural findings +
per-entity guidance entries into the lifecycle's
:class:`~specsmither.lifecycle.ports.ValidatorFinding` list.

Notes
-----

* crucible's ``validate`` is **synchronous** (the engine has no async I/O), so the
  whole seam is sync.
* :data:`_VALIDATOR_OP_TO_PATH` keys are crucible's ``OperationName`` vocabulary
  (``create_dependencies`` / ``delete_dependencies`` / ``link_blueprint_to_tickets``
  …), *not* the lifecycle's 15-op planning names — cross-validation guidance entries
  carry the validator-side op names, which this table translates into locator paths.
* Every emitted finding carries ``severity='finding'`` (advisory); crucible's own
  ``error``/``warning`` severities are not surfaced, because the gate, not the
  finding, is the hard signal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from crucible import compute_grep_candidates
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

__all__ = ["CrucibleValidatorAdapter", "FileExistenceProber", "filesystem_file_prober"]

#: Grep-evidence probe: given the candidate paths a spec needs real-repo evidence
#: for (:func:`crucible.compute_grep_candidates`), return the subset that actually
#: exists. Injected into :class:`CrucibleValidatorAdapter`; when absent the engine
#: falls back to strict spec-internal existence (brownfield modifies are flagged).
FileExistenceProber = Callable[[Sequence[str]], Iterable[str]]


def filesystem_file_prober(root: Path) -> FileExistenceProber:
    """A :data:`FileExistenceProber` rooted at ``root`` (the project working tree).

    Candidate paths are spec-relative; each is resolved against ``root`` and kept
    when it exists on disk. This is the local-first grep seam: crucible stays
    filesystem-free and this adapter supplies the real-repo evidence its
    file-provenance check needs.
    """

    def _probe(candidates: Sequence[str]) -> list[str]:
        return [c for c in candidates if (root / c).exists()]

    return _probe


# --------------------------------------------------------------------------- #
# Phase set + op→path table                                                    #
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
    "create_dependencies": "/cross-validation/by-op/create_dependencies",
    "delete_dependencies": "/cross-validation/by-op/delete_dependencies",
    "create_blueprint": "/cross-validation/by-op/create_blueprint",
    "update_blueprint": "/cross-validation/by-op/update_blueprint",
    "delete_blueprint": "/cross-validation/by-op/delete_blueprint",
    "link_blueprint_to_tickets": "/cross-validation/by-op/link_blueprint_to_tickets",
    "unlink_blueprint_to_tickets": "/cross-validation/by-op/unlink_blueprint_to_tickets",
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
        # #12 — invalid_fields feed the binary-phase `passed` verdict but were dropped
        # here, so a gate could FAIL while rendering EMPTY blockers (an invalid-enum
        # field was the invisible real blocker). Surface each as a schema finding.
        for invalid in result.structural.invalid_fields:
            findings.append(
                ValidatorFinding(
                    category=FindingCategory.SCHEMA,
                    message=f"{invalid.field_path}: {invalid.reason}",
                    severity="finding",
                    entity_id=None,
                    entity_type=None,
                    path=f"/structural/{invalid.field_path}",
                    points_lost=0.0,
                    global_impact_on_fix=0.0,
                )
            )

    # Guidance findings (rubric / cross-validation), keyed per entity.
    if result.guidance is not None:
        for entries in result.guidance.per_entity.values():
            for entry in entries:
                if isinstance(entry, GuidanceCrossValidationEntry):
                    findings.append(_cross_validation_entry_to_finding(entry))
                else:
                    findings.append(_guidance_message_to_finding(entry))

    # Cross-validation LAYER findings (requirement/NFR coverage, N/A, wave/dependency,
    # blueprint-coverage). These feed the top-level `passed` verdict but were dropped here, so a
    # phase whose entities all clear the threshold could still FAIL with EMPTY blockers — e.g.
    # epic_expansion with every epic at 100 but the spec's requirements not covered. The agent was
    # then blind to WHICH requirement ids to cover (the coverage messages carry the ids). Surface
    # them so the denial names the exact fix.
    if result.cross_validation is not None and not result.cross_validation.skipped:
        for cv in result.cross_validation.findings:
            findings.append(
                ValidatorFinding(
                    category=FindingCategory.CROSS_VALIDATION,
                    message=cv.message,
                    severity="finding",
                    entity_id=cv.primary_entity_id,
                    entity_type=None,
                    path=f"/cross-validation/{cv.category}",
                    points_lost=0.0,
                    global_impact_on_fix=0.0,
                )
            )

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

    Effectively stateless (only an optional grep prober is held) — a single
    instance is safe to share across the lifecycle. The ``config`` argument is the
    already-merged effective ``ValidatorConfig`` dict (defaults + project overrides
    + frozen spec snapshot); this adapter passes it straight through to
    :func:`crucible.validate`.

    ``file_prober`` supplies the real-repo grep evidence (``existingFiles``) the
    0.3.0 file-provenance check needs: without it, a brownfield ``filesToBeModified``
    path that no ticket creates is flagged as non-existent. The product entrypoints
    inject a :func:`filesystem_file_prober` rooted at the project working tree; when
    absent, the engine falls back to strict spec-internal existence.
    """

    def __init__(self, *, file_prober: FileExistenceProber | None = None) -> None:
        self._file_prober = file_prober

    def validate(
        self,
        spec_full: SpecFull,
        phase: PlanningPhase,
        config: Mapping[str, Any],
        language: str = "en",
    ) -> ValidatorOutput:
        """Score ``spec_full`` for ``phase`` → a flat :class:`ValidatorOutput`.

        ``language`` (default ``"en"``) is forwarded to crucible as
        ``context['language']`` so the engine's guidance prose (rubric / N/A /
        cross-validation messages) matches the lifecycle composer's language; the
        engine keeps scores, finding categories, and field paths canonical.
        """
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
            "language": language,
        }
        # Grep evidence for the file-provenance check. Tri-state: absent → strict
        # spec-internal existence; present (even empty) → E = existingFiles ∪
        # filesToBeCreated. Only probe the candidate paths crucible actually needs
        # evidence for, and only when a prober is wired (the local-first product path).
        existing_files: frozenset[str] | None = None
        if self._file_prober is not None:
            candidates = compute_grep_candidates(spec_dict)
            probed = list(self._file_prober(candidates))
            context["existingFiles"] = probed
            existing_files = frozenset(probed)
        result = cast(ValidationResult, crucible_validate(spec_dict, context))

        # gate_result is the AUTHORITATIVE per-phase verdict crucible composes
        # across all layers (structural counts for the decomposition phases,
        # the cross-validation layer for cross_validation, scoring-threshold for
        # the rubric phases) — exposed as the top-level ``result.passed``. Reading
        # only ``scoring.gate_result`` would wrongly fail every non-scoring phase,
        # making the loop unable to reach ``ready``.
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
            existing_files=existing_files,
        )
