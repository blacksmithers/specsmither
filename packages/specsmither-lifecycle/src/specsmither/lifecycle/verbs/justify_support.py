"""``justify`` / ``unjustify`` resolution — the dedicated N/A-declaration ops.

``justify`` / ``unjustify`` are the paired declare/remove ops (like
``create_dependencies`` / ``delete_dependencies``) that replace the
``update_*{fieldDeclarations}`` overload. This module is the op's pure pre-check +
target resolution: it resolves the entity by ``entityId``, validates the ``scope``
against the shared N/A-eligibility allow-set (crucible's rubric ``naEligible`` fields ∪
the cross-cutting ``dependencies`` scope) and — for ``justify`` — the reason length,
then computes the FULL merged ``fieldDeclarations`` map to persist and project.

The resolved map feeds the same spec-mutation pipeline ``update_*`` used to reach: the
``fieldDeclarations`` becomes the ``SpecMutation`` fields (persist) and the projector
merges the same map (so the in-APS gate scores the justified spec). Because it is a
dedicated op — native in every phase, never rolled back — both halves of the old
carve-out bug (fragile shape match + non-canonical key) die by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from crucible.na_eligible import NaEligibleEntity, na_eligible_scopes_for

from specsmither.lifecycle.ports import SpecFull
from specsmither.lifecycle.prechecks.result import Denied

__all__ = ["FieldDeclarations", "JustifyResolution", "resolve_justify"]

#: A ``{scope: {value: 'N/A', reason?}}`` declaration map keyed by the canonical bare scope.
FieldDeclarations = dict[str, dict[str, str]]

_EntityKind = Literal["spec", "epic", "ticket"]

#: Entity kind → the crucible ``NaEligibleEntity`` the allow-set is keyed by.
_ENTITY_TO_NA_ELIGIBLE: dict[_EntityKind, NaEligibleEntity] = {
    "spec": "specification",
    "epic": "epic",
    "ticket": "ticket",
}


@dataclass(frozen=True)
class JustifyResolution:
    """A resolved ``justify`` / ``unjustify`` call.

    ``field_declarations`` is the FULL merged map to persist (the ``SpecMutation`` SET
    replaces the whole map, so existing keys are merged in) and to project: ``justify``
    sets ``[scope] = {value: 'N/A', reason}``; ``unjustify`` deletes ``[scope]``.
    """

    entity_type: _EntityKind
    entity_id: str
    scope: str
    field_declarations: FieldDeclarations


@dataclass(frozen=True)
class _ResolvedEntity:
    entity_type: _EntityKind
    entity_id: str
    existing: FieldDeclarations


def _existing_declarations(source: Any) -> FieldDeclarations:
    """Read an entity's current ``fieldDeclarations`` map (``{}`` when absent/malformed)."""
    raw = source.get("fieldDeclarations") if isinstance(source, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
    }


def _resolve_entity(spec_full: SpecFull, entity_id: str) -> _ResolvedEntity | None:
    """Resolve an entity by id across the nested spec (ids are unique spec-wide)."""
    spec_dump = spec_full.spec.model_dump(by_alias=True)
    if spec_full.spec.id == entity_id:
        return _ResolvedEntity("spec", entity_id, _existing_declarations(spec_dump))
    for epic in spec_full.epics:
        if epic.id == entity_id:
            return _ResolvedEntity("epic", entity_id, _existing_declarations(epic.extra))
        for ticket in epic.tickets:
            if ticket.id == entity_id:
                return _ResolvedEntity("ticket", entity_id, _existing_declarations(ticket.extra))
    return None


def resolve_justify(
    op: Literal["justify", "unjustify"],
    payload: Any,
    spec_full: SpecFull,
    min_reason_length: int,
) -> JustifyResolution | Denied:
    """Validate + resolve a ``justify`` / ``unjustify`` call → resolution or denial.

    Deny reasons:

    * ``ticket_not_found``        — ``entityId`` matches no entity (reuses the not-found
      category; returns the ticket roster).
    * ``scope_not_na_eligible``   — ``scope`` is not N/A-eligible for the resolved entity.
    * ``justification_too_short`` — (``justify`` only) ``reason`` shorter than
      ``min_reason_length``.

    ``min_reason_length`` is the same bound the validator's N/A-declaration check enforces,
    so justify's accept path lines up with what the validator will score.
    """
    p = payload if isinstance(payload, dict) else {}
    raw_scope = p.get("scope")
    scope = raw_scope if isinstance(raw_scope, str) else ""
    raw_entity = p.get("entityId")
    entity_id = raw_entity if isinstance(raw_entity, str) else ""

    resolved = _resolve_entity(spec_full, entity_id)
    if resolved is None:
        tickets = [
            {"id": t.id, "epicId": e.id, "title": getattr(t, "title", "") or ""}
            for e in spec_full.epics
            for t in e.tickets
        ]
        return Denied(
            code="ticket_not_found",
            message=(
                f"{op} requires an EXISTING entity id (spec/epic/ticket). "
                "Use one of the listed ids."
            ),
            context={"ticket_id": entity_id or "(missing)", "valid_tickets": tickets},
        )

    eligible = na_eligible_scopes_for(_ENTITY_TO_NA_ELIGIBLE[resolved.entity_type])
    if scope not in eligible:
        return Denied(
            code="scope_not_na_eligible",
            message=f'Scope "{scope or "(missing)"}" is not N/A-eligible on this {resolved.entity_type}.',
            context={
                "scope": scope or "(missing)",
                "entity_id": resolved.entity_id,
                "entity_type": resolved.entity_type,
                "valid_scopes": sorted(eligible),
            },
        )

    if op == "justify":
        raw_reason = p.get("reason")
        reason = raw_reason if isinstance(raw_reason, str) else ""
        if len(reason) < min_reason_length:
            return Denied(
                code="justification_too_short",
                message=(
                    f"A justify reason must be at least {min_reason_length} characters "
                    f"(got {len(reason)})."
                ),
                context={
                    "scope": scope,
                    "entity_id": resolved.entity_id,
                    "min_length": min_reason_length,
                    "actual_length": len(reason),
                },
            )
        merged: FieldDeclarations = {**resolved.existing, scope: {"value": "N/A", "reason": reason}}
        return JustifyResolution(resolved.entity_type, resolved.entity_id, scope, merged)

    # unjustify — DELETE the scope key (no-op if absent).
    next_decls: FieldDeclarations = {k: v for k, v in resolved.existing.items() if k != scope}
    return JustifyResolution(resolved.entity_type, resolved.entity_id, scope, next_decls)
