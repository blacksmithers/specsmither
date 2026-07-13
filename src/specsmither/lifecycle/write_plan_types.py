"""The pure WritePlan value types — the lifecycle ⇄ persistence data contract.

A dependency-free leaf: the :class:`WritePlan` container, its ~13 frozen
``@dataclass`` item variants, the :data:`WritePlanItem` tagged union, and the
:data:`EntityType` / :data:`ScoreEntityType` discriminators. Importing this pulls in
only ``dataclasses`` / ``enum`` / ``typing`` — NO sqlalchemy — so the pure lifecycle
verb surface can build WritePlans without dragging the ORM in.

:mod:`specsmither.adapters.write_plan_executor` re-imports and re-exports these (for
back-compat) and keeps the sqlalchemy execution machinery (``apply_write_plan``, the
discriminator→ORM registries, ``_coerce`` …).

Shape:

* A :class:`WritePlan` is an ordered ``list[WritePlanItem]`` + a ``description``.
* :data:`WritePlanItem` is a tagged union — one frozen dataclass per kind,
  discriminated by a ``kind`` literal — over the 13 kinds in ``write-plan.ts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "ActionAppend",
    "EntityDelete",
    "EntityScoreUpdate",
    "EntityType",
    "PlanningAggregateUpsert",
    "PlanningDatapointPut",
    "RecordTransition",
    "RelatedDelete",
    "RelatedPut",
    "RelatedReplace",
    "ScoreDatapointAppend",
    "ScoreEntityType",
    "SessionCreate",
    "SessionUpdate",
    "SpecMutation",
    "WritePlan",
    "WritePlanItem",
]

#: The four entity discriminators a ``specMutation`` / ``entityDelete`` may target.
EntityType = Literal["spec", "epic", "ticket", "blueprint"]
#: The three entity discriminators a per-entity score write may target (no blueprint).
ScoreEntityType = Literal["spec", "epic", "ticket"]


# --------------------------------------------------------------------------- #
# WritePlanItem tagged union — one frozen dataclass per kind                   #
# --------------------------------------------------------------------------- #
# Every variant fixes its ``kind`` discriminator (declared last so the data fields
# stay positional / required). Dispatch is by ``isinstance`` (exhaustive, narrowed
# to ``Never`` at the tail via ``assert_never``); the literal is the wire tag the
# 0.1.0 lifecycle producer emits.


@dataclass(frozen=True)
class SpecMutation:
    """UPDATE an entity row's ``fields`` (update_spec/epic/ticket/blueprint)."""

    entity_type: EntityType
    entity_id: str
    fields: dict[str, Any]
    kind: Literal["specMutation"] = "specMutation"


@dataclass(frozen=True)
class EntityDelete:
    """Hard-delete an entity row; FK ``ON DELETE CASCADE`` drops the subtree."""

    entity_type: EntityType
    entity_id: str
    kind: Literal["entityDelete"] = "entityDelete"


@dataclass(frozen=True)
class RelatedPut:
    """INSERT (idempotent on ``id``) a row in a related/child table by logical name."""

    table: str
    item: dict[str, Any]
    kind: Literal["relatedPut"] = "relatedPut"


@dataclass(frozen=True)
class RelatedDelete:
    """DELETE the rows of a related table matching a composite ``key``."""

    table: str
    key: dict[str, str]
    kind: Literal["relatedDelete"] = "relatedDelete"


@dataclass(frozen=True)
class RelatedReplace:
    """REPLACE-ALL: delete every child where ``parent_field == parent_id``, then
    bulk-insert ``items`` (true replace — a removed entry's row is dropped)."""

    table: str
    parent_field: str
    parent_id: str
    items: list[dict[str, Any]]
    kind: Literal["relatedReplace"] = "relatedReplace"


@dataclass(frozen=True)
class SessionUpdate:
    """UPDATE a ``planning_sessions`` row's ``fields`` by id."""

    session_id: str
    fields: dict[str, Any]
    kind: Literal["sessionUpdate"] = "sessionUpdate"


@dataclass(frozen=True)
class SessionCreate:
    """INSERT a ``planning_sessions`` row (the ``session`` dict carries its id)."""

    session: dict[str, Any]
    kind: Literal["sessionCreate"] = "sessionCreate"


@dataclass(frozen=True)
class ActionAppend:
    """Append a ``planning_session_actions`` audit row."""

    action: dict[str, Any]
    kind: Literal["actionAppend"] = "actionAppend"


@dataclass(frozen=True)
class RecordTransition:
    """Append a ``planning_phase_transitions`` row."""

    transition: dict[str, Any]
    kind: Literal["recordTransition"] = "recordTransition"


@dataclass(frozen=True)
class EntityScoreUpdate:
    """Set a per-entity score. No-op in SpecSmither's schema (entities carry no
    score column — the per-entity score lives on the datapoint/aggregate); kept
    for wire parity and forward compatibility (writes ``{type}_score`` if present)."""

    entity_type: ScoreEntityType
    entity_id: str
    score: float
    kind: Literal["entityScoreUpdate"] = "entityScoreUpdate"


@dataclass(frozen=True)
class ScoreDatapointAppend:
    """Upsert a ``planning_entity_score_datapoints`` row idempotent on its composite
    id ``"{trigger_action_id}#{entity_id}#{trigger}"``; the planning-session id and
    recorded-at are derived from sibling action/session items in the same plan."""

    entity_type: ScoreEntityType
    entity_id: str
    score: float
    trigger: str
    trigger_action_id: str
    kind: Literal["scoreDatapointAppend"] = "scoreDatapointAppend"


@dataclass(frozen=True)
class PlanningAggregateUpsert:
    """Upsert the ``planning_session_aggregates`` row (PK ``planning_session_id``)."""

    aggregate: dict[str, Any]
    kind: Literal["planningAggregateUpsert"] = "planningAggregateUpsert"


@dataclass(frozen=True)
class PlanningDatapointPut:
    """Upsert a fully-formed ``planning_entity_score_datapoints`` row (carries id)."""

    datapoint: dict[str, Any]
    kind: Literal["planningDatapointPut"] = "planningDatapointPut"


#: The discriminated union the executor dispatches over (the 13 ``write-plan.ts`` kinds).
WritePlanItem = (
    SpecMutation
    | EntityDelete
    | RelatedPut
    | RelatedDelete
    | RelatedReplace
    | SessionUpdate
    | SessionCreate
    | ActionAppend
    | RecordTransition
    | EntityScoreUpdate
    | ScoreDatapointAppend
    | PlanningAggregateUpsert
    | PlanningDatapointPut
)


@dataclass(frozen=True)
class WritePlan:
    """An ordered list of :data:`WritePlanItem` + a human ``description``.

    (The TS ``transactions[][]`` chunker and the ``changes[]`` AppSync list are
    deliberately dropped — SQLite has one atomic transaction and no subscribers.)
    """

    items: list[WritePlanItem]
    description: str = ""
