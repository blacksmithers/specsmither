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
    # -- cycle-resolution guidance (the cycle_detected deny) -------------------
    "cycle.deny.frame": (
        "Adding these dependency edges would create {cycleCount} dependency cycle(s) — "
        "the whole batch was denied and nothing from it was persisted. This is INDICATIVE "
        "evidence, not a verdict: `get_ticket` the tickets in each loop and decide which "
        "ONE direction genuinely must run first. Do NOT cut blindly — a dependency can be "
        "legitimate for pure ordering even without a shared file.\n\n"
        "{cycleAnalysis}\n\n"
        "Grounding rule: an edge `X requires Y` is file-justified when X CONSUMES "
        "(references/modifies) a file Y CREATES; otherwise lean on the epic/ticket order. "
        "Keep the grounded direction and drop the spurious one — RESUBMIT "
        "`create_dependencies` without it when it is a new batch edge (`[in this batch]`), "
        "or `delete_dependencies` it when it is `[already persisted]`. Never keep a "
        "direction with no file or ordering basis."
    ),
    "cycle.shape.one-file-backed": "one direction is file-backed",
    "cycle.shape.none-file-backed": "no file backs either direction — an invented ordering",
    "cycle.shape.all-file-backed": "BOTH directions file-backed — a circular FILE dependency",
    "cycle.origin.intra-batch": "[in this batch]",
    "cycle.origin.persisted": "[already persisted]",
    "cycle.hint.forward": "the evidence favors keeping this edge",
    "cycle.hint.reverse": (
        "the evidence favors the OPPOSITE direction — likely the spurious edge"
    ),
    "cycle.hint.ambiguous": "no distinguishing signal",
    "cycle.scope.same-epic": "same epic",
    "cycle.scope.cross-epic": "cross-epic",
    "cycle.order.detailed": (
        "{scope} ({orderBasis} order: {fromId}={fromOrder}, {toId}={toOrder})"
    ),
    "cycle.file.backed": "FILE-BACKED — {fromId} consumes {files}, which {toId} creates",
    "cycle.file.none": "no file backs it",
    "cycle.edge.evidence": (
        "{fromId} → {toId} ({fromId} requires {toId}): {fileClause}; {orderClause} — "
        "{hintClause}. {originTag}"
    ),
    "cycle.getTicket": "`get_ticket {ids}`",
    "cycle.block.header": "Cycle {n}: {nodeSequence}  ({shapeLabel})",
    "cycle.block.edge": "  • {evidence}",
    "cycle.block.recovery": "  → Recovery: {recovery}",
    "cycle.overflow": "…and {count} more cycle(s).",
    "cycle.recovery.all-file-backed": (
        "{getTicket}, then reshape the file assignments with `update_ticket` — each ticket "
        "consumes a file the other creates, so this is a contradictory FILE dependency, not "
        "a spurious edge. Do NOT drop an edge (omitting one leaves a ticket consuming a file "
        "with no creator); the file fix rolls back to ticket_expansion."
    ),
    "cycle.recovery.spuriousPersisted": (
        "{getTicket} to confirm, then `delete_dependencies` the spurious edge {label} — it "
        "is already persisted and closes the loop through the existing graph. Keep the "
        "grounded direction."
    ),
    "cycle.recovery.spuriousIntraBatch": (
        "{getTicket} to confirm, then RESUBMIT `create_dependencies` OMITTING the spurious "
        "edge {label}. It is in THIS batch, so nothing was persisted — `delete_dependencies` "
        "would be a no-op here. Keep the grounded direction."
    ),
    "cycle.recovery.ambiguous": (
        "{getTicket} and decide from the tickets' content: the loop shares no justifying "
        "file{orderNote}, so keep at most ONE direction if a real execution ordering exists "
        "(or none if the tickets are independent), then {mechanism}. Do NOT keep a direction "
        "with no basis, and do NOT cut blindly."
    ),
    "cycle.recovery.mechanism.persisted": (
        "drop the direction you judge spurious — `delete_dependencies` it if it is already "
        "persisted, or OMIT it on resubmit if it is a new batch edge"
    ),
    "cycle.recovery.mechanism.intra-batch": (
        "RESUBMIT `create_dependencies` OMITTING the direction(s) you drop (nothing was "
        "persisted, so `delete_dependencies` would be a no-op)"
    ),
    "cycle.recovery.orderNote": " and no decisive order signal",
}

