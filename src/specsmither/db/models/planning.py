"""Planning-session ORM models (the 5 planning tables; architecture §3).

The planning-session entities, shaped for SpecSmither: there is no cloud-only
``projectId`` denormalization (the project is reachable via ``specification_id`` →
spec → project), userId fields collapse to the single local user, and there is no
gate-cache TTL (the validator output is still persisted for ``inspect`` /
``get_planning_status`` display but never trusted as a cache — architecture §6).

The five tables:

* :class:`PlanningSession` — the persisted aggregate root (one active per spec,
  enforced by the partial-unique lock index).
* :class:`PlanningSessionAction` — append-only audit of every attempted op.
* :class:`PlanningPhaseTransition` — append-only phase-change log.
* :class:`PlanningEntityScoreDatapoint` — append-only score time-series; its PK is
  the deterministic composite ``"{triggerActionId}#{entityId}#{trigger}"`` so the
  in-txn upsert is idempotent.
* :class:`PlanningSessionAggregate` — the materialized projection (PK =
  ``planning_session_id``), rebuilt in-txn via ``computeAggregateUpdate``.

Statuses / phases / triggers are stored as their ``StrEnum`` ``.value`` strings
(loose ``String`` columns, typed ``Mapped[str]``) so they round-trip cleanly on the
JSON wire; nullable enum columns default to ``NULL``. JSON columns use the
:class:`~specsmither.db.base.JSONType` codec.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from specsmither.db.base import Base, IdMixin, JSONType, TimestampMixin, now_iso
from specsmither.domain.enums import PlanningPhase, PlanningSessionStatus

__all__ = [
    "PlanningEntityScoreDatapoint",
    "PlanningPhaseTransition",
    "PlanningSession",
    "PlanningSessionAction",
    "PlanningSessionAggregate",
]


class PlanningSession(Base, IdMixin, TimestampMixin):
    """The planning-session aggregate root (``planning_sessions``).

    One active (``status != 'closed'``) session per specification, enforced at the
    DB by the partial-unique lock index ``ix_one_active_planning_session`` (a
    racing second ``start_planning_session`` hits a UNIQUE violation → ``CONFLICT``;
    architecture §4a). The ``last_validator_output`` JSON blob carries
    ``validatedPhase`` inside it (there is no top-level ``validated_phase`` field).
    """

    __tablename__ = "planning_sessions"

    __table_args__ = (
        Index(
            "ix_one_active_planning_session",
            "specification_id",
            unique=True,
            sqlite_where=text("status != 'closed'"),
        ),
    )

    specification_id: Mapped[str] = mapped_column(
        ForeignKey("specifications.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(default=PlanningSessionStatus.ACTIVE.value)
    current_phase: Mapped[str] = mapped_column(default=PlanningPhase.PLANNING_SPEC.value)

    started_at: Mapped[str | None] = mapped_column(default=None)
    last_action_at: Mapped[str | None] = mapped_column(default=None)
    completed_at: Mapped[str | None] = mapped_column(default=None)
    closed_at: Mapped[str | None] = mapped_column(default=None)

    last_score: Mapped[float | None] = mapped_column(default=None)
    actions_count: Mapped[int] = mapped_column(default=0)

    # Resume model: {content, recordedAt, recordedByUserId} | null.
    pending_human_feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    last_transition_trigger: Mapped[str | None] = mapped_column(default=None)
    last_transition_at: Mapped[str | None] = mapped_column(default=None)
    last_read_at: Mapped[str | None] = mapped_column(default=None)
    last_validated_at: Mapped[str | None] = mapped_column(default=None)
    last_gate_result: Mapped[str | None] = mapped_column(default=None)

    # The gate-output blob (holds validatedPhase). Shape varies → Mapped[Any].
    last_validator_output: Mapped[Any] = mapped_column(JSONType, nullable=True)

    # Latest-only denormalized guidance snapshots (read for inspect / status
    # display). These are the latest values only, not history (the history
    # lives on the action rows + aggregate folds).
    last_process_guidance: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    last_lifecycle_planning_guidance: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # `metadata` is reserved on the declarative class (Base.metadata); the attr is
    # `session_metadata`, mapped to the DB column "metadata".
    session_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONType)


class PlanningSessionAction(Base, IdMixin):
    """Append-only audit row for every attempted planning op (``planning_session_actions``).

    ``guidance_variant`` + ``findings_categories`` are the only stream source for
    the aggregate's ``guidanceVariantCounts`` / ``findingsByCategory`` folds, so
    they are persisted, not derived. The catch-all ``payload`` JSON
    absorbs the per-op detail (entityType/entityId/fieldsChanged/denyReason/
    perEntityScoresAfter/humanInstruction/targetActionId/…) that does not warrant a
    dedicated column. ``operation`` is the op name (the store filter key).
    Append-only: ``created_at`` only (no ``updated_at``).
    """

    __tablename__ = "planning_session_actions"

    planning_session_id: Mapped[str] = mapped_column(
        ForeignKey("planning_sessions.id", ondelete="CASCADE")
    )
    operation: Mapped[str | None] = mapped_column(default=None)
    phase: Mapped[str | None] = mapped_column(default=None)
    outcome: Mapped[str | None] = mapped_column(default=None)
    actor: Mapped[str | None] = mapped_column(default=None)
    guidance_variant: Mapped[str | None] = mapped_column(default=None)
    findings_categories: Mapped[list[str] | None] = mapped_column(JSONType)
    score: Mapped[float | None] = mapped_column(default=None)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=True)

    created_at: Mapped[str] = mapped_column(default=now_iso)


class PlanningPhaseTransition(Base, IdMixin):
    """Append-only phase-change log (``planning_phase_transitions``).

    ``actor`` carries who drove the transition; there is no ``triggeredByUserId`` /
    ``notes`` field (single local user). Append-only: ``created_at`` only (the
    transition timestamp).
    """

    __tablename__ = "planning_phase_transitions"

    planning_session_id: Mapped[str] = mapped_column(
        ForeignKey("planning_sessions.id", ondelete="CASCADE")
    )
    from_phase: Mapped[str | None] = mapped_column(default=None)
    to_phase: Mapped[str | None] = mapped_column(default=None)
    trigger: Mapped[str | None] = mapped_column(default=None)
    actor: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[str] = mapped_column(default=now_iso)


class PlanningEntityScoreDatapoint(Base):
    """Append-only per-entity score time-series (``planning_entity_score_datapoints``).

    The PK is the deterministic composite
    ``"{triggerActionId}#{entityId}#{trigger}"`` (NOT a ULID, no default) so the
    in-txn upsert is idempotent on the same trigger/entity/trigger triple. Mandatory
    table (it is what the Observatory/TUI score charts read). Append-only:
    ``created_at`` only (maps to ``recordedAt``).
    """

    __tablename__ = "planning_entity_score_datapoints"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    planning_session_id: Mapped[str] = mapped_column(
        ForeignKey("planning_sessions.id", ondelete="CASCADE")
    )
    trigger_action_id: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str | None] = mapped_column(default=None)
    trigger: Mapped[str] = mapped_column(String)
    score: Mapped[float | None] = mapped_column(default=None)
    phase: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[str] = mapped_column(default=now_iso)


class PlanningSessionAggregate(Base):
    """Materialized aggregate projection (``planning_session_aggregates``).

    PK = ``planning_session_id`` (also the cascading FK). A pure fold of the
    actions/transitions/datapoints, rebuilt in-txn via ``computeAggregateUpdate``;
    fully regenerable, so each rollup is a nullable JSON column. The
    ``last_processed_*`` cursors give the projector idempotency. There are no cloud
    ``projectId`` / ``specificationId`` denormalizations (reachable via the
    session). ``updated_at`` maps to ``lastUpdatedAt`` on the wire.
    """

    __tablename__ = "planning_session_aggregates"

    planning_session_id: Mapped[str] = mapped_column(
        ForeignKey("planning_sessions.id", ondelete="CASCADE"), primary_key=True
    )

    phase_timeline: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    current_scores: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    entity_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    entity_revisions: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    ops_mix: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    score_history_global: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    denied_by_reason: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    actor_mix_per_phase: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    trigger_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    guidance_variant_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    findings_by_category: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    score_delta_by_operation: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    last_processed_action_id: Mapped[str | None] = mapped_column(default=None)
    last_processed_transition_id: Mapped[str | None] = mapped_column(default=None)

    updated_at: Mapped[str] = mapped_column(default=now_iso, onupdate=now_iso)
