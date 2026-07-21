"""Lifecycle-side rendering of the CPS shared-file ``gate_not_passed`` deny's
consolidated creator-election plan.

The structural analyzer's :func:`elect_file_creators` owns the WHAT — per orphan
shared file (touched by ≥1 ticket, created by none), the elected creator (the
topologically-earliest toucher), the exact ``modifier → creator`` deps, and the
per-file acyclicity verdict (``clean`` / ``conflict``). This module owns the FLOW
prose: the cross-phase batch-it framing + the honest tool names, so the actor
resolves ALL orphan shared files as ONE structural batch (one ``ticket_expansion``
pass electing creators via ``update_ticket``, one ``cross_validation`` pass declaring
deps via ``create_dependencies``) instead of patching one file, rolling back, and
rediscovering the rest — the provenance↔ordering↔acyclicity trilemma.

Indicative, never auto-applied — a ``conflict`` degrades to a ``get_ticket``
orientation (the same graceful degradation as the all-file-backed cycle). Rendered
directly at the CPS deny site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crucible.cross_validation.creator_election import (
    CreatorElectionResult,
    FileCreatorPlan,
    FileToucher,
    ToucherRole,
)

from specsmither.lifecycle.i18n import t, text

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["format_creator_plan"]

#: The file-array field each touch-role maps to (for the ``update_ticket`` move prose).
#: Field names, canonical — never translated.
_ROLE_FIELD: dict[ToucherRole, str] = {
    "creates": "filesToBeCreated",
    "modifies": "filesToBeModified",
    "references": "filesToBeReferenced",
}


def _elected_creator_field(plan: FileCreatorPlan) -> str:
    """The field the elected creator holds the file in TODAY — the one the
    ``update_ticket`` move lifts it OUT of into ``filesToBeCreated``."""
    for toucher in plan.touchers:
        if toucher.ticket_id == plan.elected_creator:
            return _ROLE_FIELD[toucher.role]
    return _ROLE_FIELD["modifies"]


def _deps_clause(plan: FileCreatorPlan, language: str) -> str:
    """The modifier→creator deps as prose (``PAGE2 requires PAGE1, ...``)."""
    return ", ".join(
        t(language, "creator.dep", {"fromId": d.from_ticket_id, "toId": d.to_ticket_id})
        for d in plan.required_deps
    )


def _toucher_ids(touchers: Sequence[FileToucher]) -> str:
    """The toucher ids in rank order — the ``get_ticket`` argument for a conflict."""
    return " ".join(tch.ticket_id for tch in touchers)


def _format_plan_entry(plan: FileCreatorPlan, language: str) -> str:
    """One orphan file's block: elected creator + reason + the two-phase move + tag."""
    n = len(plan.touchers)
    others_keep_it = text(language, "creator.othersKeepIt") if plan.required_deps else ""
    lines = [
        t(language, f"creator.entry.file.{'one' if n == 1 else 'many'}", {"file": plan.file, "n": n}),
        t(
            language,
            "creator.entry.elect",
            {"electedCreator": plan.elected_creator, "reason": plan.reason},
        ),
        t(
            language,
            "creator.entry.move",
            {
                "electedCreator": plan.elected_creator,
                "file": plan.file,
                "fromField": _elected_creator_field(plan),
                "othersKeepIt": others_keep_it,
            },
        ),
    ]

    if plan.required_deps:
        lines.append(t(language, "creator.entry.deps", {"depsClause": _deps_clause(plan, language)}))
    else:
        lines.append(text(language, "creator.entry.noDeps"))

    if plan.status == "conflict":
        lines.append(
            t(language, "creator.entry.conflict", {"ids": _toucher_ids(plan.touchers)})
        )
    else:
        lines.append(text(language, "creator.entry.acyclic"))

    return "\n".join(lines)


def format_creator_plan(result: CreatorElectionResult, language: str) -> str:
    """Render the consolidated creator-election plan for the CPS deny.

    Returns ``""`` for an empty plan (no orphan shared files). The plan block leads
    with the batch-it / two-pass flow prose, then one entry per orphan file. Prepended
    above the flat findings the caller keeps as the per-line detail.
    """
    if not result.plans:
        return ""
    count = len(result.plans)
    header = t(
        language,
        f"creator.header.{'one' if count == 1 else 'many'}",
        {"fileCount": count},
    )
    entries = "\n".join(_format_plan_entry(plan, language) for plan in result.plans)
    return f"{header}\n\n{entries}"
