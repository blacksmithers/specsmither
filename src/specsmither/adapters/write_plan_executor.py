"""WritePlan executor — work item #16 (the lifecycle ⇄ persistence seam).

A faithful port of SpecForge's
``packages/resolvers/src/executor/write-plan-executor.ts`` (``mapItem`` kind→row
dispatch + ``run-chunks.ts``), **rewritten to one SQLite ``Session.begin()``** per
the SpecSmither architecture (decision 2, invariant 3).

What is DROPPED versus the TS original (all DynamoDB transport artifacts, per recon
A4 §1/§5): the ``transactions[][]`` chunker (DynamoDB's 100-item transactWrite cap),
the ``changes: ModelChange[]`` AppSync-subscription fan-out, the
``conditionExpression`` / idempotency ``attribute_exists`` guards, the
``ConcurrencyError`` / ``PartialWritePlanFailure`` taxonomy, and the cascade-reader
expansion (FK ``ON DELETE CASCADE`` does the work locally). What is KEPT is the only
logical content: ``mapItem``'s 13-kind dispatch, re-expressed as ORM writes.

Shape:

* A :class:`WritePlan` is an ordered ``list[WritePlanItem]`` + a ``description``.
* :data:`WritePlanItem` is a tagged union — one frozen dataclass per kind,
  discriminated by a ``kind`` literal — over the 13 kinds in ``write-plan.ts``.
* ``fields`` / ``item`` / ``key`` dict keys are ORM column (attribute) names in our
  internal **snake_case** convention; a camelCase key from a TS-shaped producer is
  normalised via :func:`to_snake` before it is matched against the model's columns.

The contract (architecture invariant 3): :func:`apply_write_plan` mutates ORM rows,
collects the set of touched specification ids, then runs the in-transaction
:func:`~specsmither.rollups.recompute.recompute` worklist — and NEVER commits. The
caller owns ``Session.begin()`` / commit. :func:`commit_write_plan` is the
convenience wrapper that opens one transaction per plan for callers that want the
full one-txn-per-mutation behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, assert_never

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from specsmither.db.base import Base, now_iso
from specsmither.db.models import (
    AcceptanceCriterion,
    Blueprint,
    CodeSnippet,
    Epic,
    EpicCommit,
    ImplementationStep,
    PlanningEntityScoreDatapoint,
    PlanningPhaseTransition,
    PlanningSession,
    PlanningSessionAction,
    PlanningSessionAggregate,
    Specification,
    SpecificationPr,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
    TicketFileChange,
    TicketTest,
    TypeSnippet,
)
from specsmither.operations.errors import NotFoundError, to_crud_error
from specsmither.rollups.recompute import recompute

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "ENTITY_MODELS",
    "TABLE_MODELS",
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
    "apply_write_plan",
    "commit_write_plan",
    "table_model",
    "to_snake",
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


# --------------------------------------------------------------------------- #
# Registries: discriminator → ORM model                                        #
# --------------------------------------------------------------------------- #

#: ``specMutation`` / ``entityDelete`` / score discriminator → core ORM model.
ENTITY_MODELS: dict[str, type[Base]] = {
    "spec": Specification,
    "epic": Epic,
    "ticket": Ticket,
    "blueprint": Blueprint,
}

#: Logical table name (the snake_case ``__tablename__`` — our internal convention) →
#: ORM model, for ``relatedPut`` / ``relatedDelete`` / ``relatedReplace``. Covers the
#: ticket child tables, the join/git rows, and the planning tables. These keys are the
#: binding contract for the 0.1.0 lifecycle WritePlan producer.
TABLE_MODELS: dict[str, type[Base]] = {
    "acceptance_criteria": AcceptanceCriterion,
    "implementation_steps": ImplementationStep,
    "ticket_file_changes": TicketFileChange,
    "ticket_tests": TicketTest,
    "code_snippets": CodeSnippet,
    "type_snippets": TypeSnippet,
    "ticket_dependencies": TicketDependency,
    "ticket_blueprint_refs": TicketBlueprintRef,
    "epic_commits": EpicCommit,
    "specification_prs": SpecificationPr,
    "planning_sessions": PlanningSession,
    "planning_session_actions": PlanningSessionAction,
    "planning_phase_transitions": PlanningPhaseTransition,
    "planning_entity_score_datapoints": PlanningEntityScoreDatapoint,
    "planning_session_aggregates": PlanningSessionAggregate,
}


def table_model(table: str) -> type[Base]:
    """Resolve a logical table name to its ORM model, or raise on an unknown name."""
    model = TABLE_MODELS.get(table)
    if model is None:
        raise ValueError(
            f"unknown WritePlan table {table!r}; known tables: {sorted(TABLE_MODELS)}"
        )
    return model


# --------------------------------------------------------------------------- #
# key normalisation + field filtering                                          #
# --------------------------------------------------------------------------- #

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")

#: Producer key (already snake-cased) → ORM attribute name where they diverge.
#: ``metadata`` is reserved on the declarative class, so PlanningSession maps the
#: ``metadata`` column to the ``session_metadata`` attribute.
_COLUMN_ALIASES = {"metadata": "session_metadata"}

#: The parent-linkage columns the affected-spec resolver inspects, in precedence order.
_LINKAGE_ATTRS = ("specification_id", "ticket_id", "epic_id", "planning_session_id")


def to_snake(name: str) -> str:
    """Normalise a (possibly camelCase) producer key to snake_case (idempotent)."""
    stage = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    return _CAMEL_BOUNDARY_2.sub(r"\1_\2", stage).lower()


@cache
def _column_attrs(model: type[Base]) -> frozenset[str]:
    """The model's mapped attribute names (cached; ``setattr`` / constructor targets)."""
    return frozenset(model.__mapper__.column_attrs.keys())


def _coerce(value: Any) -> Any:
    """Coerce an enum member to its plain ``.value`` (passthrough otherwise)."""
    if isinstance(value, Enum):
        return value.value
    return value


def _snake_keys(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``raw`` with every key snake-cased + aliased (no column filtering)."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        snake = to_snake(key)
        out[_COLUMN_ALIASES.get(snake, snake)] = value
    return out


def _filtered(model: type[Base], raw: Mapping[str, Any]) -> dict[str, Any]:
    """``raw`` snake-cased, value-coerced, and restricted to the model's columns.

    Restricting to known mapped attributes makes both the ``setattr`` update path
    and the ``Model(**data)`` insert path tolerant of a producer that carries extra
    (e.g. cloud-only) keys.
    """
    attrs = _column_attrs(model)
    out: dict[str, Any] = {}
    for key, value in raw.items():
        snake = to_snake(key)
        snake = _COLUMN_ALIASES.get(snake, snake)
        if snake in attrs:
            out[snake] = _coerce(value)
    return out


# --------------------------------------------------------------------------- #
# affected-spec resolution (each touched entity → its owning specification id)  #
# --------------------------------------------------------------------------- #


def _spec_id_for_entity(session: Session, entity_type: str, entity_id: str) -> str | None:
    """Resolve a core entity to its owning spec id: ticket→epic→spec, epic→spec,
    spec=self, blueprint→spec. Returns ``None`` if the row is gone."""
    if entity_type == "spec":
        return entity_id
    if entity_type == "epic":
        epic = session.get(Epic, entity_id)
        return epic.specification_id if epic is not None else None
    if entity_type == "ticket":
        ticket = session.get(Ticket, entity_id)
        if ticket is None:
            return None
        epic = session.get(Epic, ticket.epic_id)
        return epic.specification_id if epic is not None else None
    if entity_type == "blueprint":
        blueprint = session.get(Blueprint, entity_id)
        return blueprint.specification_id if blueprint is not None else None
    return None


def _resolve_spec_from_data(session: Session, data: Mapping[str, Any]) -> str | None:
    """Resolve the owning spec id from a row's linkage columns (precedence order)."""
    spec_id = data.get("specification_id")
    if spec_id:
        return str(spec_id)
    ticket_id = data.get("ticket_id")
    if ticket_id:
        return _spec_id_for_entity(session, "ticket", str(ticket_id))
    epic_id = data.get("epic_id")
    if epic_id:
        return _spec_id_for_entity(session, "epic", str(epic_id))
    planning_session_id = data.get("planning_session_id")
    if planning_session_id:
        ps = session.get(PlanningSession, str(planning_session_id))
        return ps.specification_id if ps is not None else None
    return None


def _resolve_spec_from_row(session: Session, row: Base) -> str | None:
    """Resolve the owning spec id from a live ORM row's linkage attributes."""
    data: dict[str, Any] = {
        attr: getattr(row, attr) for attr in _LINKAGE_ATTRS if hasattr(row, attr)
    }
    return _resolve_spec_from_data(session, data)


def _add(affected: set[str], spec_id: str | None) -> None:
    if spec_id is not None:
        affected.add(spec_id)


# --------------------------------------------------------------------------- #
# derived context (fills scoreDatapointAppend from sibling items, TS parity)    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Context:
    planning_session_id: str | None
    recorded_at: str


def _derive_context(plan: WritePlan, generated_at: str | None) -> _Context:
    """Scrape ``planning_session_id`` + ``recorded_at`` from sibling action/session
    items, mirroring the TS ``deriveContext`` — ``scoreDatapointAppend`` carries no
    session metadata of its own."""
    planning_session_id: str | None = None
    recorded_at: str | None = None
    for item in plan.items:
        if isinstance(item, ActionAppend):
            action = _snake_keys(item.action)
            planning_session_id = planning_session_id or action.get("planning_session_id")
            recorded_at = recorded_at or action.get("performed_at") or action.get("created_at")
        elif isinstance(item, SessionCreate):
            planning_session_id = planning_session_id or _snake_keys(item.session).get("id")
        elif isinstance(item, SessionUpdate):
            planning_session_id = planning_session_id or item.session_id
    psid = str(planning_session_id) if planning_session_id is not None else None
    return _Context(psid, recorded_at or generated_at or now_iso())


# --------------------------------------------------------------------------- #
# row writers                                                                  #
# --------------------------------------------------------------------------- #


def _put_row(
    session: Session, model: type[Base], data: dict[str, Any], *, pk_attr: str = "id"
) -> Base:
    """Idempotent put: update in place when ``data[pk_attr]`` already exists, else
    insert (a missing/``None`` pk lets the ULID default generate one)."""
    pk = data.get(pk_attr)
    if pk is not None:
        existing = session.get(model, pk)
        if existing is not None:
            for attr, value in data.items():
                if attr != pk_attr:
                    setattr(existing, attr, value)
            return existing
    row = model(**data)
    session.add(row)
    return row


# --------------------------------------------------------------------------- #
# per-kind dispatch (the rewritten ``mapItem``)                                 #
# --------------------------------------------------------------------------- #


def _apply_item(
    session: Session, item: WritePlanItem, affected: set[str], context: _Context
) -> None:
    """Map one :data:`WritePlanItem` to its ORM write and record the touched spec."""
    if isinstance(item, SpecMutation):
        model = ENTITY_MODELS[item.entity_type]
        row = session.get(model, item.entity_id)
        if row is None:
            raise NotFoundError(
                f"{item.entity_type} {item.entity_id!r} not found",
                context={"kind": item.entity_type, "id": item.entity_id},
            )
        for attr, value in _filtered(model, item.fields).items():
            setattr(row, attr, value)
        _add(affected, _spec_id_for_entity(session, item.entity_type, item.entity_id))

    elif isinstance(item, EntityDelete):
        model = ENTITY_MODELS[item.entity_type]
        row = session.get(model, item.entity_id)
        if row is None:
            return  # idempotent: already gone.
        _add(affected, _spec_id_for_entity(session, item.entity_type, item.entity_id))
        session.delete(row)  # FK ON DELETE CASCADE drops the subtree.

    elif isinstance(item, RelatedPut):
        model = table_model(item.table)
        data = _filtered(model, item.item)
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, model, data)

    elif isinstance(item, RelatedDelete):
        model = table_model(item.table)
        key = _filtered(model, item.key)
        for row in session.execute(select(model).filter_by(**key)).scalars().all():
            _add(affected, _resolve_spec_from_row(session, row))
            session.delete(row)

    elif isinstance(item, RelatedReplace):
        model = table_model(item.table)
        snake = to_snake(item.parent_field)
        parent_field = _COLUMN_ALIASES.get(snake, snake)
        _add(affected, _resolve_spec_from_data(session, {parent_field: item.parent_id}))
        column = getattr(model, parent_field)
        for row in session.execute(select(model).where(column == item.parent_id)).scalars().all():
            session.delete(row)
        session.flush()  # land the deletes before the inserts (avoid UNIQUE clashes).
        for raw in item.items:
            data = _filtered(model, raw)
            data.setdefault(parent_field, item.parent_id)
            session.add(model(**data))

    elif isinstance(item, SessionCreate):
        data = _filtered(PlanningSession, item.session)
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningSession, data)

    elif isinstance(item, SessionUpdate):
        row = session.get(PlanningSession, item.session_id)
        if row is None:
            raise NotFoundError(
                f"planning_session {item.session_id!r} not found",
                context={"kind": "planning_session", "id": item.session_id},
            )
        for attr, value in _filtered(PlanningSession, item.fields).items():
            setattr(row, attr, value)
        _add(affected, _resolve_spec_from_row(session, row))

    elif isinstance(item, ActionAppend):
        data = _filtered(PlanningSessionAction, item.action)
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningSessionAction, data)

    elif isinstance(item, RecordTransition):
        data = _filtered(PlanningPhaseTransition, item.transition)
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningPhaseTransition, data)

    elif isinstance(item, EntityScoreUpdate):
        model = ENTITY_MODELS[item.entity_type]
        row = session.get(model, item.entity_id)
        if row is not None:
            score_attr = f"{item.entity_type}_score"
            if score_attr in _column_attrs(model):  # absent in M0 schema → no-op.
                setattr(row, score_attr, item.score)
        _add(affected, _spec_id_for_entity(session, item.entity_type, item.entity_id))

    elif isinstance(item, ScoreDatapointAppend):
        if context.planning_session_id is None:
            raise ValueError(
                "scoreDatapointAppend requires a planning session id derivable from a "
                "sibling actionAppend / sessionCreate / sessionUpdate item in the plan"
            )
        datapoint_id = f"{item.trigger_action_id}#{item.entity_id}#{item.trigger}"
        data = {
            "id": datapoint_id,
            "planning_session_id": context.planning_session_id,
            "trigger_action_id": item.trigger_action_id,
            "entity_id": item.entity_id,
            "entity_type": item.entity_type,
            "trigger": item.trigger,
            "score": item.score,
            "created_at": context.recorded_at,
        }
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningEntityScoreDatapoint, data)

    elif isinstance(item, PlanningDatapointPut):
        data = _filtered(PlanningEntityScoreDatapoint, item.datapoint)
        if data.get("id") is None:
            raise ValueError("planningDatapointPut requires the datapoint to carry an id")
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningEntityScoreDatapoint, data)

    elif isinstance(item, PlanningAggregateUpsert):
        data = _filtered(PlanningSessionAggregate, item.aggregate)
        _add(affected, _resolve_spec_from_data(session, data))
        _put_row(session, PlanningSessionAggregate, data, pk_attr="planning_session_id")

    else:  # pragma: no cover - exhaustiveness guard.
        assert_never(item)


# --------------------------------------------------------------------------- #
# public surface                                                               #
# --------------------------------------------------------------------------- #


def apply_write_plan(
    session: Session, plan: WritePlan, *, generated_at: str | None = None
) -> None:
    """Apply ``plan``'s items in order onto ``session``, then run the recompute worklist.

    Iterates the items, mapping each kind to its ORM write and collecting the set of
    specification ids the mutation touched (each entity resolved to its owning spec:
    ticket→epic→spec, epic→spec, spec=self, blueprint→spec, child/related rows and
    dependency edges via their parent). After every item is applied and flushed, calls
    :func:`~specsmither.rollups.recompute.recompute` for those specs — materialising
    cascaded statuses, counts and the dependency tree **in the same transaction**.

    A :class:`~sqlalchemy.exc.IntegrityError` (FK / NOT NULL / UNIQUE …) raised while
    applying or flushing the items is translated to its semantic
    :class:`~specsmither.operations.errors.CrudError` via
    :func:`~specsmither.operations.errors.to_crud_error`.

    Does NOT commit — the caller owns the transaction (architecture invariant 3).
    """
    affected: set[str] = set()
    context = _derive_context(plan, generated_at)
    try:
        for item in plan.items:
            _apply_item(session, item, affected, context)
        session.flush()
    except IntegrityError as exc:
        raise to_crud_error(exc) from exc
    recompute(session, spec_ids=affected, generated_at=generated_at)


def commit_write_plan(
    session_factory: sessionmaker[Session],
    plan: WritePlan,
    *,
    generated_at: str | None = None,
) -> None:
    """Open one ``Session.begin()`` and :func:`apply_write_plan` ``plan`` inside it.

    The full one-transaction-per-mutation behaviour: ``BEGIN IMMEDIATE`` (the
    single-writer write lock), apply + recompute, COMMIT — or roll the whole plan back
    on any error. For callers that already own a transaction, use
    :func:`apply_write_plan` directly.
    """
    with session_factory.begin() as session:
        apply_write_plan(session, plan, generated_at=generated_at)
