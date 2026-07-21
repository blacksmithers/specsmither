"""Audit-action row builder.

A **pure** factory for the ``planning_session_actions`` row payload that an L4 verb
wraps in an :class:`~specsmither.adapters.write_plan_executor.ActionAppend` WritePlan
item — one append per attempted op (success or denied).

What the M0 schema makes dedicated columns vs. the catch-all
-----------------------------------------------------------

The M0 ``PlanningSessionAction`` model keeps only a handful of dedicated columns
(``operation`` / ``phase`` / ``outcome`` / ``actor`` / ``guidance_variant`` /
``findings_categories`` / ``score``) plus the catch-all ``payload`` JSON. Everything
else — the op ``payload`` itself, the derived ``entity_type`` / ``entity_id`` /
``fields_changed``, and
``deny_reason`` / ``deny_details`` / ``per_entity_scores_after`` /
``human_instruction`` / ``target_action_id`` / ``performed_by_user_id`` — is merged
**flat** into ``payload`` so the M0 aggregate fold can read both the op-payload keys
(``dependencies`` / ``ids`` / ``title`` / ``fields`` …) and the audit extras
(``entity_id`` / ``deny_reason`` / ``per_entity_scores_after`` …) off the one dict.

The two stream-fold sources (the CRITICAL contract)
---------------------------------------------------

``guidance_variant`` + ``findings_categories`` are the **sole** source the M0
``planning_session_aggregates`` ``guidanceVariantCounts`` / ``findingsByCategory``
folds read — they are stamped here at construction, never re-derived downstream. Omit
them and those aggregates come out empty.

Where the categories come from
------------------------------

L3 runs *before* the L4 guidance composer, so this builder takes the
``guidance_variant`` the caller already chose and derives ``findings_categories``
directly from ``validator_output.findings`` (the distinct, order-preserving category
set), rather than reading a composed guidance's grouped blocks. ``score`` is
``validator_output.local_score``. When no ``validator_output`` is supplied (synthetic
ops such as ``phase_advance`` / a pre-validation denial) both ``score`` and
``findings_categories`` are omitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from specsmither.domain.enums import ActorType, GuidanceVariant, Outcome, PlanningPhase
from specsmither.ids import new_ulid, now_iso
from specsmither.lifecycle.ports import Clock, IdGenerator, ValidatorOutput

__all__ = [
    "build_action",
]


#: Operation → audited ``entity_type``.
_ENTITY_TYPE_BY_OP: dict[str, str] = {
    "update_spec": "spec",
    "create_epic": "epic",
    "update_epic": "epic",
    "delete_epic": "epic",
    "create_ticket": "ticket",
    "update_ticket": "ticket",
    "delete_ticket": "ticket",
    "create_blueprint": "blueprint",
    "update_blueprint": "blueprint",
    "delete_blueprint": "blueprint",
    "link_blueprint_to_tickets": "blueprint",
    "unlink_blueprint_to_tickets": "blueprint",
    "create_dependencies": "ticket",
    "delete_dependencies": "ticket",
}


def _v(value: Any) -> Any:
    """Coerce an :class:`enum.Enum` member to its plain ``.value`` (passthrough else)."""
    return value.value if isinstance(value, Enum) else value


def _mint(id_generator: IdGenerator | None) -> str:
    return (id_generator or new_ulid)()


def _now(clock: Clock | None) -> str:
    return now_iso() if clock is None else clock().isoformat()


def _extract_entity_fields(operation: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Derive ``entity_type`` / ``entity_id`` / ``fields_changed`` from the payload.

    ``entity_type`` from the op map; ``entity_id`` from
    ``payload.id`` (update/delete) falling back to the blueprint id (``blueprint_id``
    / ``blueprintId`` for the link/unlink ops); ``fields_changed`` from the keys of
    ``payload.fields``. Any absent piece is simply omitted.
    """

    if not isinstance(payload, Mapping):
        return {}

    out: dict[str, Any] = {}

    entity_type = _ENTITY_TYPE_BY_OP.get(operation)
    if entity_type:
        out["entity_type"] = entity_type

    entity_id: str | None = None
    raw_id = payload.get("id")
    if isinstance(raw_id, str):
        entity_id = raw_id
    else:
        raw_bp = payload.get("blueprint_id")
        if not isinstance(raw_bp, str):
            raw_bp = payload.get("blueprintId")
        if isinstance(raw_bp, str):
            entity_id = raw_bp
    if entity_id:
        out["entity_id"] = entity_id

    fields = payload.get("fields")
    if isinstance(fields, Mapping):
        out["fields_changed"] = list(fields.keys())

    return out


def _distinct_finding_categories(validator_output: ValidatorOutput) -> list[str]:
    """Order-preserving distinct ``category`` set of the validator findings.

    Read off ``validator_output.findings`` (each finding's ``category``) rather than a
    composed guidance's grouped blocks. Each category is coerced to its plain string
    value.
    """

    seen: set[str] = set()
    categories: list[str] = []
    for finding in validator_output.findings:
        category = _v(finding.category)
        if category not in seen:
            seen.add(category)
            categories.append(category)
    return categories


def build_action(
    *,
    session_id: str,
    operation: str,
    phase: PlanningPhase | str | None,
    outcome: Outcome | str,
    actor: ActorType | str,
    validator_output: ValidatorOutput | None = None,
    guidance_variant: GuidanceVariant | str | None = None,
    payload: Mapping[str, Any] | None = None,
    deny_reason: str | None = None,
    deny_details: Mapping[str, Any] | None = None,
    per_entity_scores_after: Mapping[str, float] | None = None,
    human_instruction: str | None = None,
    target_action_id: str | None = None,
    user_id: str | None = None,
    id_generator: IdGenerator | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Build a ``planning_session_actions`` row dict (``buildAction``, lines 34-60).

    The returned dict is the payload for an ``ActionAppend`` WritePlan item. Dedicated
    columns: ``id`` (fresh ULID / ``id_generator()``), ``planning_session_id``,
    ``operation``, ``phase``, ``outcome``, ``actor``, ``created_at`` (ISO-8601 via
    ``now_iso`` / ``clock()``). Stamped only when present: ``guidance_variant`` (the
    caller's chosen variant); ``findings_categories`` (the distinct
    ``validator_output.findings`` categories, omitted when empty); ``score``
    (``validator_output.local_score``, omitted when no validator output). All other
    per-op detail — the op ``payload`` plus ``entity_type`` / ``entity_id`` /
    ``fields_changed`` / ``deny_reason`` / ``deny_details`` /
    ``per_entity_scores_after`` / ``human_instruction`` / ``target_action_id`` /
    ``performed_by_user_id`` — is merged flat into the catch-all ``payload`` column.
    """

    row: dict[str, Any] = {
        "id": _mint(id_generator),
        "planning_session_id": session_id,
        "operation": operation,
        "phase": _v(phase),
        "outcome": _v(outcome),
        "actor": _v(actor),
    }

    if guidance_variant is not None:
        row["guidance_variant"] = _v(guidance_variant)

    if validator_output is not None:
        row["score"] = validator_output.local_score
        categories = _distinct_finding_categories(validator_output)
        if categories:
            row["findings_categories"] = categories

    # The catch-all ``payload`` column: the op payload spread flat, then the audit
    # extras merged on top (so the aggregate fold reads both off one dict).
    catch_all: dict[str, Any] = {}
    if payload is not None:
        catch_all.update(payload)
    catch_all.update(_extract_entity_fields(operation, payload))
    if deny_reason is not None:
        catch_all["deny_reason"] = deny_reason
    if deny_details is not None:
        catch_all["deny_details"] = dict(deny_details)
    if per_entity_scores_after is not None:
        catch_all["per_entity_scores_after"] = dict(per_entity_scores_after)
    if human_instruction is not None:
        catch_all["human_instruction"] = human_instruction
    if target_action_id is not None:
        catch_all["target_action_id"] = target_action_id
    if user_id is not None:
        catch_all["performed_by_user_id"] = user_id
    if catch_all:
        row["payload"] = catch_all

    row["created_at"] = _now(clock)
    return row
