"""Planning-session aggregate + score-datapoint folds (pure) — port of the TS.

Ports ``packages/resolvers/src/aggregators/planning/{compute-datapoints,
compute-aggregate-update,replay-compute}.ts`` (recon A4). The DynamoDB-stream
parser / dedup / chunked-WritePlan machinery (``stream-event-parser.ts``,
``deduplication.ts``, ``project-stream-batch.ts``, ``apply-aggregate.ts``) is
intentionally dropped: SpecSmither runs this fold *in the mutation transaction*
(architecture decision 2), so there is no stream to parse and idempotency comes
from the deterministic datapoint id rather than a stream cursor + filter.

Everything here is a **pure fold** — no ORM, no I/O, no module-scope clock. The
inputs are lightweight frozen dataclasses (:class:`Action`, :class:`Transition`,
:class:`SessionContext`); the JSON projection groups are typed dicts that map
1:1 onto the :class:`~specsmither.db.models.PlanningSessionAggregate` JSON columns
(``phase_timeline``, ``current_scores``, ``entity_counts``, … plus the
``last_processed_*`` cursors). The L4 in-txn writer adapts these onto the ORM row
(dropping the cloud-only ``project_id`` / ``specification_id`` denorms, which are
reachable via the session).

Three public folds:

* :func:`compute_datapoints` — actions → score datapoints. The id is the
  deterministic composite ``f"{trigger_action_id}#{entity_id}#{trigger}"`` so
  re-deriving the same ``(action, entity, trigger)`` triple yields the SAME id
  and the in-txn upsert collapses to a no-op instead of a duplicate row.
* :func:`compute_aggregate_update` — the incremental fold: clone the previous
  aggregate (or seed an empty one), apply new transitions then new actions, emit
  this batch's datapoints, fold them into ``current_scores.per_entity`` (last
  wins), and advance the ``last_processed_*`` cursors to the max id seen.
* :func:`replay_compute` — rebuild the whole aggregate from the empty seed by
  applying every action + transition. The **replay invariant**: a single-pass
  replay equals the incremental fold applied in sequence (transitions and actions
  are independent projection dimensions, so any chunking that preserves intra-kind
  order is associative).

Determinism notes: ``max_id`` is a plain lexicographic max (ULID ids sort
chronologically); ``current_scores.per_entity`` and ``current_scores.global``
are last-wins; ``score_delta_by_operation`` seeds its "previous score" from the
persisted ``current_scores.global`` so deltas stay correct across batches.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

from specsmither.domain.enums import FindingCategory, GuidanceVariant

__all__ = [
    "Action",
    "ActorMix",
    "Aggregate",
    "AggregateUpdate",
    "CurrentScores",
    "Datapoint",
    "DatapointEntityType",
    "EntityCounts",
    "EntityRevision",
    "PerEntityScore",
    "PhaseTimelineEntry",
    "RegistryEntityType",
    "ReplayResult",
    "ScoreDelta",
    "ScoreHistoryEntry",
    "SessionContext",
    "Transition",
    "compute_aggregate_update",
    "compute_datapoints",
    "create_empty_aggregate",
    "datapoint_id",
    "replay_compute",
]

#: Entity type as registered in ``entity_revisions`` (wider than the datapoint's
#: own type, which excludes ``blueprint``).
RegistryEntityType = Literal["spec", "epic", "ticket", "blueprint"]
#: Subset valid as a datapoint ``entity_type``.
DatapointEntityType = Literal["spec", "epic", "ticket"]
#: Subset an ``update_*`` / ``create_*`` action may bump a revision for.
RevisionEntityType = Literal["epic", "ticket", "blueprint"]

_ActorLiteral = Literal["agent", "human"]
_OutcomeLiteral = Literal["success", "denied"]

#: ``createEmptyAggregate`` seeds ``last_updated_at`` to the epoch (TS ``new
#: Date(0)``); every real fold overwrites it with the injected clock.
_EPOCH_ISO = "1970-01-01T00:00:00+00:00"

_GUIDANCE_VARIANT_SET = frozenset(v.value for v in GuidanceVariant)
_FINDING_CATEGORY_SET = frozenset(c.value for c in FindingCategory)


# ---------------------------------------------------------------------------
# JSON projection group shapes (map 1:1 to the PlanningSessionAggregate columns)
# ---------------------------------------------------------------------------


class PhaseTimelineEntry(TypedDict):
    """One ``phase_timeline`` segment (``exited_at`` / ``duration_ms`` are set
    when the next transition closes the entry)."""

    phase: str
    entered_at: str
    exited_at: NotRequired[str]
    duration_ms: NotRequired[int]


class PerEntityScore(TypedDict):
    """A ``current_scores.per_entity`` value (latest datapoint wins)."""

    type: DatapointEntityType
    score: float


CurrentScores = TypedDict(
    "CurrentScores",
    {"global": NotRequired[float], "per_entity": dict[str, PerEntityScore]},
)
"""``current_scores`` group: an optional ``global`` (latest scored action) plus
the per-entity latest scores. ``global`` is a Python keyword, so this group uses
the functional ``TypedDict`` syntax to keep the verbatim key."""


class EntityCounts(TypedDict):
    """Live ``entity_counts`` (floored at 0 on delete)."""

    epic: int
    ticket: int
    blueprint: int
    dependency: int


class EntityRevision(TypedDict):
    """An ``entity_revisions`` value: type + latest title + edit count."""

    type: RevisionEntityType
    title: str
    count: int
    last_revised_at: str


class ScoreHistoryEntry(TypedDict):
    """A ``score_history_global`` point."""

    at: str
    score: float
    trigger_action_id: str


class ActorMix(TypedDict):
    """An ``actor_mix_per_phase`` bucket: agent vs human action tallies."""

    agent: int
    human: int


class ScoreDelta(TypedDict):
    """A ``score_delta_by_operation`` accumulator (mean = ``sum / count``)."""

    sum: float
    count: int


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Session identity threaded through the folds (seeds + datapoint context)."""

    planning_session_id: str
    project_id: str
    specification_id: str


@dataclass(frozen=True, slots=True)
class Action:
    """A planning-session action row (the fold's append-only input).

    Subset of ``PlanningSessionAction`` the projector reads. ``payload`` is the
    catch-all blob (``title`` / ``fields.title`` for revision titles, and
    ``dependencies`` / ``ids`` for the dependency-count inference).
    """

    id: str
    planning_session_id: str
    performed_at: str
    phase: str
    operation: str
    outcome: _OutcomeLiteral = "success"
    performed_by_actor: _ActorLiteral = "agent"
    entity_type: str | None = None
    entity_id: str | None = None
    payload: Any = None
    deny_reason: str | None = None
    score_after: float | None = None
    per_entity_scores_after: dict[str, float] | None = None
    guidance_variant: str | None = None
    findings_categories: list[str] | None = None


@dataclass(frozen=True, slots=True)
class Transition:
    """A phase-transition row (drives ``phase_timeline`` + ``trigger_breakdown``)."""

    id: str
    planning_session_id: str
    triggered_at: str
    from_phase: str
    to_phase: str
    trigger: str


@dataclass(frozen=True, slots=True)
class Datapoint:
    """A per-entity score datapoint with the deterministic composite id."""

    id: str
    entity_id: str
    entity_type: DatapointEntityType
    planning_session_id: str
    project_id: str
    recorded_at: str
    score: float
    trigger_action_id: str
    trigger: str


@dataclass
class Aggregate:
    """The materialized projection — the in-memory shape of one
    ``planning_session_aggregates`` row.

    Mutable: :func:`compute_aggregate_update` clones the previous aggregate and
    mutates the clone (the input is never touched). Every collection field maps
    to a JSON column; the two cursors + ``last_updated_at`` are scalar columns.
    """

    planning_session_id: str
    phase_timeline: list[PhaseTimelineEntry] = field(default_factory=list)
    current_scores: CurrentScores = field(
        default_factory=lambda: CurrentScores(per_entity={})
    )
    entity_counts: EntityCounts = field(
        default_factory=lambda: EntityCounts(epic=0, ticket=0, blueprint=0, dependency=0)
    )
    entity_revisions: dict[str, EntityRevision] = field(default_factory=dict)
    ops_mix: dict[str, int] = field(default_factory=dict)
    score_history_global: list[ScoreHistoryEntry] = field(default_factory=list)
    denied_by_reason: dict[str, int] = field(default_factory=dict)
    actor_mix_per_phase: dict[str, ActorMix] = field(default_factory=dict)
    trigger_breakdown: dict[str, int] = field(default_factory=dict)
    guidance_variant_counts: dict[str, int] = field(default_factory=dict)
    findings_by_category: dict[str, int] = field(default_factory=dict)
    score_delta_by_operation: dict[str, ScoreDelta] = field(default_factory=dict)
    last_processed_action_id: str | None = None
    last_processed_transition_id: str | None = None
    last_updated_at: str = _EPOCH_ISO


@dataclass(frozen=True, slots=True)
class AggregateUpdate:
    """Output of one incremental fold: the next aggregate + emitted datapoints."""

    aggregate: Aggregate
    datapoints: list[Datapoint]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Output of a full replay: the rebuilt aggregate, all datapoints, and the
    number of chunks applied (``1`` unless ``batch_size`` splits the replay)."""

    aggregate: Aggregate
    datapoints: list[Datapoint]
    batches: int


@dataclass(frozen=True, slots=True)
class _DatapointCtx:
    project_id: str
    specification_id: str
    entity_registry: Mapping[str, RegistryEntityType]


# ---------------------------------------------------------------------------
# Datapoints (compute-datapoints.ts)
# ---------------------------------------------------------------------------


def datapoint_id(trigger_action_id: str, entity_id: str, trigger: str) -> str:
    """The deterministic composite datapoint id (the idempotent upsert key)."""
    return f"{trigger_action_id}#{entity_id}#{trigger}"


def compute_datapoints(
    actions: Sequence[Action],
    *,
    project_id: str,
    specification_id: str,
    entity_registry: Mapping[str, RegistryEntityType],
) -> list[Datapoint]:
    """Pure: actions → datapoints. Only ``success`` actions emit; the op →
    trigger mapping is the v0.1.0 greenfield contract (see
    :func:`_append_datapoints_for_action`). Re-running over the same actions is
    byte-identical because every id is :func:`datapoint_id`-derived.
    """
    ctx = _DatapointCtx(project_id, specification_id, entity_registry)
    out: list[Datapoint] = []
    for action in actions:
        if action.outcome != "success":
            continue
        _append_datapoints_for_action(out, action, ctx)
    return out


def _append_datapoints_for_action(
    out: list[Datapoint], action: Action, ctx: _DatapointCtx
) -> None:
    op = action.operation
    if op == "create_epic":
        _single(out, action, ctx, "created", "epic", action.entity_id)
    elif op == "create_ticket":
        _single(out, action, ctx, "created", "ticket", action.entity_id)
    elif op == "update_spec":
        _single(out, action, ctx, "metadata_updated", "spec", action.entity_id or ctx.specification_id)
    elif op == "update_epic":
        _single(out, action, ctx, "epic_field_updated", "epic", action.entity_id)
    elif op == "update_ticket":
        _single(out, action, ctx, "ticket_field_updated", "ticket", action.entity_id)
    elif op == "update_blueprint":
        _emit_per_affected(out, action, ctx, "blueprint_field_updated")
    elif op == "create_dependencies":
        _emit_per_affected(out, action, ctx, "dependency_added")
    elif op == "delete_dependencies":
        _emit_per_affected(out, action, ctx, "dependency_removed")
    elif op == "link_blueprint_to_tickets":
        _emit_per_affected(out, action, ctx, "blueprint_linked")
    elif op == "unlink_blueprint_to_tickets":
        _emit_per_affected(out, action, ctx, "blueprint_unlinked")
    elif op in ("phase_complete", "phase_advance") and action.phase == "cross_validation":
        # Cross-validation full refresh: score every affected entity.
        _emit_per_affected(out, action, ctx, "cross_val_recompute")
    # All other ops (delete_*, create_blueprint, synthetic) emit no datapoint.


def _single(
    out: list[Datapoint],
    action: Action,
    ctx: _DatapointCtx,
    trigger: str,
    entity_type: DatapointEntityType,
    entity_id: str | None,
) -> None:
    if not entity_id:
        return
    score = _score_for_entity(action, entity_id)
    if score is None:
        return
    out.append(_make_datapoint(action, ctx, trigger, entity_type, entity_id, score))


def _emit_per_affected(
    out: list[Datapoint], action: Action, ctx: _DatapointCtx, trigger: str
) -> None:
    scores = action.per_entity_scores_after
    if not scores:
        return
    for entity_id, score in scores.items():
        entity_type = _resolve_entity_type(entity_id, ctx)
        if entity_type is None:
            continue
        out.append(_make_datapoint(action, ctx, trigger, entity_type, entity_id, score))


def _make_datapoint(
    action: Action,
    ctx: _DatapointCtx,
    trigger: str,
    entity_type: DatapointEntityType,
    entity_id: str,
    score: float,
) -> Datapoint:
    return Datapoint(
        id=datapoint_id(action.id, entity_id, trigger),
        entity_id=entity_id,
        entity_type=entity_type,
        planning_session_id=action.planning_session_id,
        project_id=ctx.project_id,
        recorded_at=action.performed_at,
        score=score,
        trigger_action_id=action.id,
        trigger=trigger,
    )


def _score_for_entity(action: Action, entity_id: str) -> float | None:
    per_entity = action.per_entity_scores_after
    if per_entity is not None:
        value = per_entity.get(entity_id)
        if value is not None:
            return value
    return action.score_after


def _resolve_entity_type(entity_id: str, ctx: _DatapointCtx) -> DatapointEntityType | None:
    if entity_id == ctx.specification_id:
        return "spec"
    registered = ctx.entity_registry.get(entity_id)
    if registered == "blueprint":
        return None  # blueprints aren't a datapoint entity type
    if registered == "spec":
        return "spec"
    if registered == "epic":
        return "epic"
    # 'ticket', None, or an unknown id — ticket is the most common per-entity
    # score key in the planning surface (cross-validation scores every ticket).
    return "ticket"


# ---------------------------------------------------------------------------
# Incremental fold (compute-aggregate-update.ts)
# ---------------------------------------------------------------------------


def create_empty_aggregate(ctx: SessionContext) -> Aggregate:
    """Seed a fresh aggregate for a session (epoch ``last_updated_at``)."""
    return Aggregate(planning_session_id=ctx.planning_session_id)


def compute_aggregate_update(
    previous_aggregate: Aggregate | None,
    actions: Sequence[Action] = (),
    transitions: Sequence[Transition] = (),
    *,
    session_context: SessionContext,
    now: Callable[[], str],
) -> AggregateUpdate:
    """Apply new ``actions`` + ``transitions`` to ``previous_aggregate`` (or a
    fresh seed) and return the next aggregate plus the datapoints the new actions
    emit.

    The previous aggregate is never mutated (it is cloned first). Datapoints are
    produced *here* (not an input) because their entity-type registry is built
    from the post-action ``entity_revisions``; the caller persists the returned
    datapoints alongside the aggregate. Cursors advance to the max id seen; a
    kind with no new items leaves its cursor untouched.
    """
    seed = previous_aggregate if previous_aggregate is not None else create_empty_aggregate(
        session_context
    )
    nxt = _clone_aggregate(seed)

    # 1. Transitions → phase_timeline + trigger_breakdown.
    _apply_transitions(nxt, transitions)

    # 2. Actions → ops_mix, entity_counts, entity_revisions, scores, breakdowns.
    _apply_actions(nxt, actions)

    # 3. Datapoints from the new actions (registry built from post-action state).
    registry = _build_registry(nxt, session_context.specification_id)
    datapoints = compute_datapoints(
        actions,
        project_id=session_context.project_id,
        specification_id=session_context.specification_id,
        entity_registry=registry,
    )

    # 4. Datapoints → current_scores.per_entity (latest wins).
    _apply_datapoints_to_per_entity(nxt, datapoints)

    # 5. Cursors + clock.
    last_action_id = _max_id(actions)
    if last_action_id is not None:
        nxt.last_processed_action_id = last_action_id
    last_transition_id = _max_id(transitions)
    if last_transition_id is not None:
        nxt.last_processed_transition_id = last_transition_id
    nxt.last_updated_at = now()

    return AggregateUpdate(aggregate=nxt, datapoints=datapoints)


def _clone_aggregate(a: Aggregate) -> Aggregate:
    cloned_scores: CurrentScores = {
        "per_entity": {k: v.copy() for k, v in a.current_scores["per_entity"].items()}
    }
    global_score = a.current_scores.get("global")
    if global_score is not None:
        cloned_scores["global"] = global_score
    return Aggregate(
        planning_session_id=a.planning_session_id,
        phase_timeline=[e.copy() for e in a.phase_timeline],
        current_scores=cloned_scores,
        entity_counts=a.entity_counts.copy(),
        entity_revisions={k: v.copy() for k, v in a.entity_revisions.items()},
        ops_mix=dict(a.ops_mix),
        score_history_global=[e.copy() for e in a.score_history_global],
        denied_by_reason=dict(a.denied_by_reason),
        actor_mix_per_phase={k: v.copy() for k, v in a.actor_mix_per_phase.items()},
        trigger_breakdown=dict(a.trigger_breakdown),
        guidance_variant_counts=dict(a.guidance_variant_counts),
        findings_by_category=dict(a.findings_by_category),
        score_delta_by_operation={k: v.copy() for k, v in a.score_delta_by_operation.items()},
        last_processed_action_id=a.last_processed_action_id,
        last_processed_transition_id=a.last_processed_transition_id,
        last_updated_at=a.last_updated_at,
    )


# --- transitions -----------------------------------------------------------


def _apply_transitions(agg: Aggregate, transitions: Sequence[Transition]) -> None:
    for t in transitions:
        entered_at = t.triggered_at
        agg.trigger_breakdown[t.trigger] = agg.trigger_breakdown.get(t.trigger, 0) + 1
        if not agg.phase_timeline:
            # auto_initial seed: record the toPhase as the first active entry.
            agg.phase_timeline.append(PhaseTimelineEntry(phase=t.to_phase, entered_at=entered_at))
            continue
        _close_active_entry(agg, entered_at)
        # Reopening the same phase (rollback) is legitimate — always append a
        # fresh active entry.
        agg.phase_timeline.append(PhaseTimelineEntry(phase=t.to_phase, entered_at=entered_at))


def _close_active_entry(agg: Aggregate, exited_at: str) -> None:
    if not agg.phase_timeline:
        return
    last = agg.phase_timeline[-1]
    if last.get("exited_at"):
        return  # already closed
    last["exited_at"] = exited_at
    entered_ms = _parse_ms(last["entered_at"])
    exited_ms = _parse_ms(exited_at)
    if entered_ms is not None and exited_ms is not None:
        last["duration_ms"] = max(0, exited_ms - entered_ms)


def _parse_ms(value: str) -> int | None:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


# --- actions ---------------------------------------------------------------


def _apply_actions(agg: Aggregate, actions: Sequence[Action]) -> None:
    # score_delta_by_operation folds consecutive scoreAfter deltas keyed by
    # operation; the "previous" score must persist across batches, so seed it
    # from current_scores.global (the last scoreAfter the aggregate has seen).
    prev_score = agg.current_scores.get("global")

    for a in actions:
        agg.ops_mix[a.operation] = agg.ops_mix.get(a.operation, 0) + 1
        # Every action (any outcome) folds into actor mix / guidance / findings.
        _fold_actor_mix(agg, a)
        _fold_guidance_variant(agg, a)
        _fold_findings_categories(agg, a)

        if a.outcome == "denied":
            if a.deny_reason:
                agg.denied_by_reason[a.deny_reason] = (
                    agg.denied_by_reason.get(a.deny_reason, 0) + 1
                )
            continue

        _apply_entity_counts(agg, a)
        _apply_entity_revision(agg, a)

        score_after = a.score_after
        if score_after is not None:
            if prev_score is not None:
                delta = score_after - prev_score
                acc = agg.score_delta_by_operation.get(a.operation)
                if acc is None:
                    acc = ScoreDelta(sum=0.0, count=0)
                agg.score_delta_by_operation[a.operation] = ScoreDelta(
                    sum=acc["sum"] + delta, count=acc["count"] + 1
                )
            prev_score = score_after
            agg.current_scores["global"] = score_after
            agg.score_history_global.append(
                ScoreHistoryEntry(at=a.performed_at, score=score_after, trigger_action_id=a.id)
            )


def _fold_actor_mix(agg: Aggregate, a: Action) -> None:
    bucket = agg.actor_mix_per_phase.get(a.phase)
    if bucket is None:
        bucket = ActorMix(agent=0, human=0)
    if a.performed_by_actor == "human":
        bucket["human"] += 1
    else:
        bucket["agent"] += 1
    agg.actor_mix_per_phase[a.phase] = bucket


def _fold_guidance_variant(agg: Aggregate, a: Action) -> None:
    variant = a.guidance_variant
    # Guard against rows whose captured variant predates an enum value: only
    # fold known variants so the aggregate stays schema-valid.
    if not variant or variant not in _GUIDANCE_VARIANT_SET:
        return
    agg.guidance_variant_counts[variant] = agg.guidance_variant_counts.get(variant, 0) + 1


def _fold_findings_categories(agg: Aggregate, a: Action) -> None:
    categories = a.findings_categories
    if not categories:
        return
    for cat in categories:
        if cat not in _FINDING_CATEGORY_SET:
            continue
        agg.findings_by_category[cat] = agg.findings_by_category.get(cat, 0) + 1


def _apply_entity_counts(agg: Aggregate, a: Action) -> None:
    op = a.operation
    ec = agg.entity_counts
    if op == "create_epic":
        ec["epic"] += 1
    elif op == "delete_epic":
        ec["epic"] = max(0, ec["epic"] - 1)
    elif op == "create_ticket":
        ec["ticket"] += 1
    elif op == "delete_ticket":
        ec["ticket"] = max(0, ec["ticket"] - 1)
    elif op == "create_blueprint":
        ec["blueprint"] += 1
    elif op == "delete_blueprint":
        ec["blueprint"] = max(0, ec["blueprint"] - 1)
    elif op == "create_dependencies":
        ec["dependency"] += _infer_dependency_count(a)
    elif op == "delete_dependencies":
        ec["dependency"] = max(0, ec["dependency"] - _infer_dependency_count(a))


def _infer_dependency_count(a: Action) -> int:
    """Best-effort count from a ``create_/delete_dependencies`` payload."""
    payload = a.payload
    if isinstance(payload, dict):
        deps = payload.get("dependencies")
        if isinstance(deps, list):
            return len(deps)
        ids = payload.get("ids")
        if isinstance(ids, list):
            return len(ids)
    return 1


def _apply_entity_revision(agg: Aggregate, a: Action) -> None:
    if not a.entity_id:
        return
    revision_type = _map_entity_type_for_revision(a.operation)
    if revision_type is None:
        return
    existing = agg.entity_revisions.get(a.entity_id)
    title = _extract_title(a)
    if title is None:
        title = existing["title"] if existing is not None else ""
    count = (existing["count"] if existing is not None else 0) + 1
    agg.entity_revisions[a.entity_id] = EntityRevision(
        type=revision_type, title=title, count=count, last_revised_at=a.performed_at
    )


def _map_entity_type_for_revision(operation: str) -> RevisionEntityType | None:
    if operation in ("create_epic", "update_epic"):
        return "epic"
    if operation in ("create_ticket", "update_ticket"):
        return "ticket"
    if operation in ("create_blueprint", "update_blueprint"):
        return "blueprint"
    # delete_* and others don't bump revision counts.
    return None


def _extract_title(a: Action) -> str | None:
    payload = a.payload
    if isinstance(payload, dict):
        title = payload.get("title")
        if isinstance(title, str):
            return title
        fields = payload.get("fields")
        if isinstance(fields, dict):
            nested = fields.get("title")
            if isinstance(nested, str):
                return nested
    return None


# --- datapoints → per-entity scores ----------------------------------------


def _apply_datapoints_to_per_entity(agg: Aggregate, datapoints: Sequence[Datapoint]) -> None:
    # Latest wins; datapoints are emitted in action (chronological) order.
    for d in datapoints:
        agg.current_scores["per_entity"][d.entity_id] = PerEntityScore(
            type=d.entity_type, score=d.score
        )


# --- registry / utility ----------------------------------------------------


def _build_registry(agg: Aggregate, specification_id: str) -> dict[str, RegistryEntityType]:
    registry: dict[str, RegistryEntityType] = {specification_id: "spec"}
    for entity_id, revision in agg.entity_revisions.items():
        registry[entity_id] = revision["type"]
    return registry


def _max_id(items: Sequence[Action] | Sequence[Transition]) -> str | None:
    if not items:
        return None
    return max(item.id for item in items)


# ---------------------------------------------------------------------------
# Full replay (replay-compute.ts)
# ---------------------------------------------------------------------------


def replay_compute(
    actions: Sequence[Action],
    transitions: Sequence[Transition] = (),
    *,
    session_context: SessionContext,
    now: Callable[[], str],
    batch_size: int | None = None,
) -> ReplayResult:
    """Rebuild the whole aggregate from the empty seed.

    Single-pass by default. When ``batch_size`` is set the replay is split into
    chunks of that many actions (transitions partitioned by id range to align
    with the action batches); chunked replay must produce the same aggregate as
    a single pass (the associativity / replay invariant), and the concatenated
    datapoints are equal because ids are deterministic and order is preserved.
    """
    if not batch_size or batch_size <= 0:
        result = compute_aggregate_update(
            None, actions, transitions, session_context=session_context, now=now
        )
        return ReplayResult(aggregate=result.aggregate, datapoints=result.datapoints, batches=1)

    current: Aggregate | None = None
    all_datapoints: list[Datapoint] = []
    batches = 0
    transition_cursor = 0

    for i in range(0, len(actions), batch_size):
        action_batch = actions[i : i + batch_size]
        cutoff_id = action_batch[-1].id
        transition_end = transition_cursor
        while transition_end < len(transitions) and transitions[transition_end].id <= cutoff_id:
            transition_end += 1
        transition_batch = transitions[transition_cursor:transition_end]
        transition_cursor = transition_end

        result = compute_aggregate_update(
            current, action_batch, transition_batch, session_context=session_context, now=now
        )
        current = result.aggregate
        all_datapoints.extend(result.datapoints)
        batches += 1

    # Flush any trailing transitions (those after the last action's id range).
    if transition_cursor < len(transitions):
        result = compute_aggregate_update(
            current, (), transitions[transition_cursor:], session_context=session_context, now=now
        )
        current = result.aggregate
        all_datapoints.extend(result.datapoints)
        batches += 1

    # Edge case: no actions at all — apply transitions in a single batch.
    if current is None:
        result = compute_aggregate_update(
            None, (), transitions, session_context=session_context, now=now
        )
        return ReplayResult(aggregate=result.aggregate, datapoints=result.datapoints, batches=1)

    return ReplayResult(aggregate=current, datapoints=all_datapoints, batches=batches)
