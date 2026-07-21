"""Canonical English guidance strings, keyed for translation overlay.

Every string the lifecycle guidance composer emits resolves through this catalog
(via :func:`specsmither.lifecycle.i18n.text`), so a translation catalog can
override any of them by key. The English here is the byte-exact prose the
composer has always emitted — moving a string into this catalog must never
change EN output.

Field-instruction prose (phase goal/character, per-field description, interview
hooks, N/A conditions) is NOT here: its English source of truth stays in the
per-phase ``guidance/catalogs/<phase>.yaml`` files; translations overlay them via
the sibling ``<phase>.<language>.yaml`` catalogs.
"""

from __future__ import annotations

TEXT_EN: dict[str, str] = {
    # -- phase display names (the composer's PHASE_META catalog) ---------------
    "phase.name.planning_spec": "Spec Definition",
    "phase.name.epic_decomposition": "Epic Decomposition",
    "phase.name.epic_expansion": "Epic Expansion",
    "phase.name.ticket_decomposition": "Ticket Decomposition",
    "phase.name.ticket_expansion": "Ticket Expansion",
    "phase.name.cross_validation": "Cross-Validation",
    "phase.name.planned": "Planned",
    # -- small inline labels --------------------------------------------------
    "label.none": "(none)",
    "label.notScored": "not yet scored",
    "label.thresholdNa": "n/a",
    "label.gateUnknown": "unknown",
    "label.finalized": "the specification is finalized (planning complete)",
    "label.advancesTo": "the session advances to {phase}",
    # -- structured side-block prose ------------------------------------------
    "findings.summary": "{count} finding(s) across {categories} category/categories.",
    # -- variant bodies (compose_body) ----------------------------------------
    "body.gatePassed": (
        "Phase {idx} of 6 — {human}: the gate is passing (score {score}, threshold "
        "{threshold}). Keep refining via {native}, or call `complete_planning_session` "
        "to hand the specification to a human reviewer."
    ),
    "body.gatePassed.nextSuffix": (
        " {nextCount} entity/entities are listed for an optional final pass."
    ),
    "body.gateFailed": (
        "Phase {idx} of 6 — {human}: the gate is failing (score {score}, threshold "
        "{threshold}). Address the {findingCount} outstanding finding(s) via {native}, "
        "then re-run the operation. See the recommended moves for the exact verb per "
        "finding."
    ),
    "body.denied": "Denied: {message}",
    "body.denied.blockers": " Blocking reasons: {blockers}.",
    "body.denied.fallbackMessage": "The operation was denied.",
    "body.phaseAdvance": (
        "Advanced to phase {idx} of 6 — {human}. Work this phase via {native}; "
        "re-validate as you go and watch the gate before completing."
    ),
    "body.phaseRollback": (
        "Rolled back to phase {idx} of 6 — {human} to apply a structural change native "
        "to that phase. Re-establish a passing gate via {native} before advancing again."
    ),
    "body.humanHandover": (
        "The specification is ready for human review. Share it with a reviewer and poll "
        "`get_planning_status` for their approve / reject decision."
    ),
    "body.awaitingHumanReview": (
        "Awaiting human review. The specification is parked for a reviewer — do not keep "
        "editing unless asked. Poll `get_planning_status` for their decision. On "
        "approval: {nextPhaseLabel}."
    ),
    "body.humanFeedback": (
        "Human feedback received:{quoted} Incorporate it via {native} in {human}, then "
        "re-validate and call `complete_planning_session` again."
    ),
    "body.humanFeedback.quoted": ' "{content}"',
    "body.humanRejected": (
        "The human rejected the handover without written feedback. Ask them what needs "
        "rework, address it via {native} in {human}, then call `complete_planning_session` "
        "again."
    ),
    "body.sessionClosed": (
        "Planning session closed. Final phase {human}, score {score}, {actionsCount} "
        "action(s) recorded. The specification is ready."
    ),
    "body.statusReport": (
        "Phase {idx} of 6 — {human}: gate {gate}, score {score} (threshold "
        "{threshold}). {hint} Native operation(s): {native}."
    ),
    "body.statusReport.hintPass": (
        "You may continue refining via the native operation(s), or call "
        "`complete_planning_session` to hand off to the human."
    ),
    "body.statusReport.hintFail": (
        "Address the outstanding findings via the native operation(s), then call "
        "`complete_planning_session`."
    ),
    "body.statusReport.hintUnknown": "Run any native operation to get an initial score.",
    # -- field-instruction scaffolding (field_instructions renderer) -----------
    # The phase-content prose (goal / character / per-field description / hooks /
    # na_when) lives in the per-phase YAML catalogs and is translated by the sibling
    # ``<phase>.<language>.yaml`` overlays; these keys are the structural frame.
    "field.header": (
        "You are in phase {idx} of 6: {name}.\n"
        "Goal: {goal}\n"
        "Character: {character}\n"
        "\n"
        "What to do in this phase:"
    ),
    "field.noneFillable": "(no fillable fields in this phase)",
    "field.bulletNone": "(none)",
    "field.required": "(required)",
    "field.optional": "(optional)",
    "field.minCount": "    - Minimum count: {minCount}",
    "field.tier": "    - Tier: {tier}",
    "field.render": (
        "For **`{field}`** {requiredLabel}:\n"
        "- Shape: `{shape}`\n"
        "- {description}\n"
        "- Interview hooks:\n"
        "  {hooks}\n"
        "- {naClause}\n"
        "{minCountClause}\n"
        "{tierClause}\n"
        "\n"
        "Examples:\n"
        "{examples}\n"
    ),
    "field.naNotEligible": "Not N/A-eligible — must be filled.",
    "field.naWhen": " when {naWhen}",
    "field.naEligible": (
        'N/A-eligible{when}: declare via `update_*` with '
        '`fieldDeclarations: { "{field}": '
        '{ "value": "N/A", "reason": "<≥20 chars>" } }`.'
    ),
}
