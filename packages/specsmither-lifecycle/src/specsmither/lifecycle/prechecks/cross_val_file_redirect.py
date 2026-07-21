"""Post-projection pre-check: redirect an ordering-only late ``update_ticket`` to
``create_dependencies`` instead of rolling the session back.

A late ``update_ticket`` in ``cross_validation`` normally rolls the session back to
``ticket_expansion``. For the ONE case where the fix is a pure ORDERING gap — the consumed
file already exists and only the dependency edge on its creator is missing (file-provenance
invariant #2, "consume-ordered") — a rollback is wrong: ``create_dependencies`` (native to
``cross_validation``) is the real fix. So this blocks the op with a ``use_create_dependencies``
redirect and keeps the session in ``cross_validation``.

The redirect is FINDING-conditioned, not payload-shape-conditioned: it reads the per-path
provenance findings over the PROJECTED post-update spec and redirects ONLY when EVERY touched
path carries a live consume-ordered (#2) finding AND NO concurrent-modification (#6) finding —
a #6 path prescribes ticket rework (a rollback), never the "just add a dep" shortcut. Any other
case (a new/deleted file, a body field, a #6-covered path, or a path with no ordering finding)
falls through to the rollback. When in doubt, roll back — never silently drop the op.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible import check_concurrent_modification, check_file_provenance
from crucible.cross_validation.file_provenance import FileProvenanceContext

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["cross_val_file_redirect"]

#: The two ORDERED consumer roles (invariant #2). ``filesToBeDeleted`` (#5) and
#: ``filesToBeCreated`` (#1/#3) are excluded — touching either makes the update a
#: structural change that must roll back.
_CONSUME_ROLE_FIELDS: tuple[str, ...] = ("filesToBeReferenced", "filesToBeModified")


def _creator_of(spec: Mapping[str, Any], path: str) -> str:
    """The in-spec creator of ``path`` — the creating ticket, else the first modifier, else ''.

    Same precedence as the file-provenance model (create wins; modify is the brownfield
    stand-in). For an APS redirect the path is always ticket-created, so create resolves it.
    """
    tickets = [t for epic in spec.get("epics", []) or [] for t in (epic.get("tickets") or [])]
    for ticket in tickets:
        if path in (ticket.get("filesToBeCreated") or []):
            return str(ticket.get("id", ""))
    for ticket in tickets:
        if path in (ticket.get("filesToBeModified") or []):
            return str(ticket.get("id", ""))
    return ""


def cross_val_file_redirect(
    op: str,
    current_phase: PlanningPhase,
    payload: Mapping[str, Any],
    projected_spec: Mapping[str, Any],
    config: Any,
    existing_files: frozenset[str] | None = None,
) -> PrecheckResult:
    """Redirect an ordering-only late ``update_ticket`` → ``create_dependencies``.

    ``projected_spec`` is the crucible wire dict of the POST-update spec. ``existing_files``
    is the grep evidence (absent on the APS path → strict ``E = createdPaths``, which still
    fires consume-ordered for ticket-created paths — the redirect's target).
    """
    if op != "update_ticket" or current_phase != PlanningPhase.CROSS_VALIDATION:
        return Accepted()

    raw_id = payload.get("id")
    ticket_id = raw_id if isinstance(raw_id, str) and raw_id else None
    if ticket_id is None:
        return Accepted()

    fields = payload.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        return Accepted()
    # SHAPE gate (necessary, not sufficient): the update touches ONLY ordered consume-role
    # file arrays. Any other field is a structural change that must roll back.
    if not all(key in _CONSUME_ROLE_FIELDS for key in fields):
        return Accepted()

    touched: dict[str, None] = {}  # insertion-ordered set
    for key in _CONSUME_ROLE_FIELDS:
        arr = fields.get(key)
        if isinstance(arr, list):
            for entry in arr:
                if isinstance(entry, str):
                    touched[entry] = None
    if not touched:
        return Accepted()

    ctx = FileProvenanceContext(existing_files=existing_files)
    provenance = [e.finding for e in check_file_provenance(dict(projected_spec), config, ctx)]
    concurrent = [e.finding for e in check_concurrent_modification(dict(projected_spec))]

    concurrent_paths = {
        f.context["path"]
        for f in concurrent
        if isinstance(f.context, dict) and isinstance(f.context.get("path"), str)
    }
    # consume-ordered (#2) finding fields for THIS ticket — an exact field lookup avoids
    # parsing a path that could itself contain a colon.
    ordered_fields = {
        f.field
        for f in provenance
        if f.category == "file-provenance"
        and "create_dependencies" in f.operations
        and f.primary_entity_id == ticket_id
    }

    def _is_ordered(path: str) -> bool:
        return (
            f"tickets[id={ticket_id}].filesToBeReferenced:{path}" in ordered_fields
            or f"tickets[id={ticket_id}].filesToBeModified:{path}" in ordered_fields
        )

    redirect_path: str | None = None
    for path in touched:
        if path in concurrent_paths:
            return Accepted()  # #6 → rollback
        if not _is_ordered(path):
            return Accepted()  # not a #2 ordering gap → rollback
        if redirect_path is None:
            redirect_path = path
    if redirect_path is None:
        return Accepted()

    creator = _creator_of(projected_spec, redirect_path)
    return Denied(
        code="use_create_dependencies",
        message=(
            f"'{redirect_path}' already exists — the only gap is the ordering edge on its "
            f"creator{f' ticket {creator}' if creator else ''}. Add it with 'create_dependencies' "
            "(this ticket depends on the creator) instead of editing the ticket, which would roll "
            "the session back to ticket_expansion."
        ),
        context={"path": redirect_path, "creator_ticket_id": creator},
    )
