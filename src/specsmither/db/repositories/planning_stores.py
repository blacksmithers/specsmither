"""The 5 planning ``*StoreSqlite`` repositories (architecture §4/§13; recon A6).

These mirror the SpecForge ``session-types`` planning store interfaces
(``IPlanningSession*Store``) over SQLite, drop-in via the same DI bag. Every store
is **session-bound** (subclasses :class:`SessionStore`, holds the caller's
``Session``) and runs inside the caller's ``Session.begin()`` — it never opens or
commits a transaction.

Read/write split (architecture §4/§13):

* :class:`PlanningSessionStore`, :class:`PlanningSessionActionStore`,
  :class:`PlanningPhaseTransitionStore` are **READ-ONLY** in SpecSmither. Planning
  mutation (session create/update, action append, phase transition) flows through
  the 0.1.0 ``WritePlan`` executor, not these stores — exactly as the JSON
  behavioural template documents (*"planning writes flow through
  @specforge/lifecycle WritePlan + executor"*). The lifecycle is not built here.
* :class:`PlanningEntityScoreDatapointStore` and
  :class:`PlanningSessionAggregateStore` additionally carry an **in-transaction
  upsert** — the write path the L4 rollup (:mod:`specsmither.rollups.aggregate`)
  uses to materialize the score time-series + the aggregate projection. Both
  upserts are idempotent on a deterministic primary key, so re-running the rollup
  over the same actions updates in place and never duplicates.

Projection economy (architecture §4): the TS ``ForDashboard`` / ``ByProject``
projection variants collapse to one ``get_*`` + one ``list_*`` (with optional
filters) each; the cloud ``projectId`` denormalization is dropped (the project is
reachable via ``specification_id`` → spec → project). No denormalized **count**
columns are written here — those are owned by the recompute worklist (#14). The
planning store interfaces expose no count delta-mutators, so there are none to
no-op.

Existence seam: a by-own-id ``get`` resolves the authoritative row or raises
``NotFoundError`` via :func:`require_found`; the query-shaped lookups
(``get_active_for_spec``, ``get_aggregate``) return ``None`` when nothing matches
(the aggregate is a regenerable projection that may not exist until the first
rollup). Reads/writes operate on the ORM models directly (consistent — there is no
``domain.records`` DTO for the planning entities).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from specsmither.db.models import (
    PlanningEntityScoreDatapoint,
    PlanningPhaseTransition,
    PlanningSession,
    PlanningSessionAction,
    PlanningSessionAggregate,
)
from specsmither.db.repositories.base import SessionStore, require_found
from specsmither.domain.enums import PlanningSessionStatus

__all__ = [
    "PlanningEntityScoreDatapointStore",
    "PlanningPhaseTransitionStore",
    "PlanningSessionActionStore",
    "PlanningSessionAggregateStore",
    "PlanningSessionStore",
]


class PlanningSessionStore(SessionStore):
    """Read-only planning-session repository (writes go through the WritePlan executor)."""

    def get_planning_session(self, planning_session_id: str) -> PlanningSession:
        """Resolve a session by id, or raise ``NotFoundError``."""
        row = self.session.get(PlanningSession, planning_session_id)
        return require_found(row, kind="planning_session", entity_id=planning_session_id)

    def get_active_for_spec(self, specification_id: str) -> PlanningSession | None:
        """The single non-closed session for ``specification_id`` (or ``None``).

        At most one can exist — the partial-unique lock index
        ``ix_one_active_planning_session`` (``status != 'closed'``) enforces it.
        """
        stmt = (
            select(PlanningSession)
            .where(
                PlanningSession.specification_id == specification_id,
                PlanningSession.status != PlanningSessionStatus.CLOSED.value,
            )
            .order_by(PlanningSession.id.desc())
        )
        return self.session.execute(stmt).scalars().first()

    def list_planning_sessions(
        self, specification_id: str | None = None
    ) -> list[PlanningSession]:
        """Sessions, newest first (ULID id), optionally filtered by spec."""
        stmt = select(PlanningSession)
        if specification_id is not None:
            stmt = stmt.where(PlanningSession.specification_id == specification_id)
        stmt = stmt.order_by(PlanningSession.id.desc())
        return list(self.session.execute(stmt).scalars())


class PlanningSessionActionStore(SessionStore):
    """Read-only append-only audit-action repository (writes via the WritePlan executor)."""

    def get_action(self, action_id: str) -> PlanningSessionAction:
        """Resolve an action by id, or raise ``NotFoundError``."""
        row = self.session.get(PlanningSessionAction, action_id)
        return require_found(row, kind="planning_session_action", entity_id=action_id)

    def list_actions(
        self,
        planning_session_id: str | None = None,
        *,
        operation: str | None = None,
        outcome: str | None = None,
        phase: str | None = None,
    ) -> list[PlanningSessionAction]:
        """Actions in chronological order (ULID id = performed-at), with optional filters."""
        stmt = select(PlanningSessionAction)
        if planning_session_id is not None:
            stmt = stmt.where(PlanningSessionAction.planning_session_id == planning_session_id)
        if operation is not None:
            stmt = stmt.where(PlanningSessionAction.operation == operation)
        if outcome is not None:
            stmt = stmt.where(PlanningSessionAction.outcome == outcome)
        if phase is not None:
            stmt = stmt.where(PlanningSessionAction.phase == phase)
        stmt = stmt.order_by(PlanningSessionAction.id)
        return list(self.session.execute(stmt).scalars())


class PlanningPhaseTransitionStore(SessionStore):
    """Read-only append-only phase-transition repository (writes via the WritePlan executor)."""

    def get_transition(self, transition_id: str) -> PlanningPhaseTransition:
        """Resolve a transition by id, or raise ``NotFoundError``."""
        row = self.session.get(PlanningPhaseTransition, transition_id)
        return require_found(
            row, kind="planning_phase_transition", entity_id=transition_id
        )

    def list_transitions(
        self,
        planning_session_id: str | None = None,
        *,
        trigger: str | None = None,
    ) -> list[PlanningPhaseTransition]:
        """Transitions in chronological order (ULID id), optionally filtered by trigger."""
        stmt = select(PlanningPhaseTransition)
        if planning_session_id is not None:
            stmt = stmt.where(
                PlanningPhaseTransition.planning_session_id == planning_session_id
            )
        if trigger is not None:
            stmt = stmt.where(PlanningPhaseTransition.trigger == trigger)
        stmt = stmt.order_by(PlanningPhaseTransition.id)
        return list(self.session.execute(stmt).scalars())


class PlanningEntityScoreDatapointStore(SessionStore):
    """Score-datapoint time-series: read methods + the idempotent in-txn upsert.

    The mandatory append-only series the rollup writes and the Observatory/TUI
    charts read.
    """

    def get_datapoint(self, datapoint_id: str) -> PlanningEntityScoreDatapoint:
        """Resolve a datapoint by its composite id, or raise ``NotFoundError``."""
        row = self.session.get(PlanningEntityScoreDatapoint, datapoint_id)
        return require_found(
            row, kind="planning_entity_score_datapoint", entity_id=datapoint_id
        )

    def list_datapoints(
        self,
        planning_session_id: str | None = None,
        *,
        entity_id: str | None = None,
        entity_type: str | None = None,
        trigger: str | None = None,
    ) -> list[PlanningEntityScoreDatapoint]:
        """Datapoints in chronological order (ULID-free composite id sorts stably), filtered."""
        stmt = select(PlanningEntityScoreDatapoint)
        if planning_session_id is not None:
            stmt = stmt.where(
                PlanningEntityScoreDatapoint.planning_session_id == planning_session_id
            )
        if entity_id is not None:
            stmt = stmt.where(PlanningEntityScoreDatapoint.entity_id == entity_id)
        if entity_type is not None:
            stmt = stmt.where(PlanningEntityScoreDatapoint.entity_type == entity_type)
        if trigger is not None:
            stmt = stmt.where(PlanningEntityScoreDatapoint.trigger == trigger)
        stmt = stmt.order_by(PlanningEntityScoreDatapoint.id)
        return list(self.session.execute(stmt).scalars())

    def upsert_datapoint(
        self,
        *,
        planning_session_id: str,
        trigger_action_id: str,
        entity_id: str,
        trigger: str,
        entity_type: str | None = None,
        score: float | None = None,
        phase: str | None = None,
        created_at: str | None = None,
    ) -> PlanningEntityScoreDatapoint:
        """In-txn upsert idempotent on the composite PK.

        The id is the deterministic ``"{trigger_action_id}#{entity_id}#{trigger}"``
        triple (``INSERT ... ON CONFLICT(id) DO UPDATE`` semantics via get-or-set):
        re-upserting the same triple updates the row in place, never duplicates. The
        identity columns are fixed on insert; the mutable projection
        (``entity_type`` / ``score`` / ``phase``) refreshes on every call. Runs
        inside the caller's transaction — no flush/commit here.
        """
        datapoint_id = f"{trigger_action_id}#{entity_id}#{trigger}"
        row = self.session.get(PlanningEntityScoreDatapoint, datapoint_id)
        if row is None:
            row = PlanningEntityScoreDatapoint(
                id=datapoint_id,
                planning_session_id=planning_session_id,
                trigger_action_id=trigger_action_id,
                entity_id=entity_id,
                trigger=trigger,
            )
            self.session.add(row)
        row.entity_type = entity_type
        row.score = score
        row.phase = phase
        if created_at is not None:
            row.created_at = created_at
        return row


#: The materialized projection columns :meth:`PlanningSessionAggregateStore.upsert_aggregate`
#: may write. Excludes the identity PK (``planning_session_id``) and ``updated_at``
#: (auto-stamped via the model's ``onupdate``). Never includes a denormalized entity
#: count column — those are the recompute worklist's (#14), not this projection's.
_AGGREGATE_PROJECTION_COLUMNS = frozenset(
    {
        "phase_timeline",
        "current_scores",
        "entity_counts",
        "entity_revisions",
        "ops_mix",
        "score_history_global",
        "denied_by_reason",
        "actor_mix_per_phase",
        "trigger_breakdown",
        "guidance_variant_counts",
        "findings_by_category",
        "score_delta_by_operation",
        "last_processed_action_id",
        "last_processed_transition_id",
    }
)


class PlanningSessionAggregateStore(SessionStore):
    """Materialized aggregate projection: read + the idempotent in-txn upsert.

    A pure fold of the actions/transitions/datapoints, rebuilt in-transaction by
    the L4 rollup. Fully regenerable, so it may legitimately not exist yet.
    """

    def get_aggregate(self, planning_session_id: str) -> PlanningSessionAggregate | None:
        """The aggregate for a session, or ``None`` (regenerable; may not exist yet)."""
        return self.session.get(PlanningSessionAggregate, planning_session_id)

    def upsert_aggregate(
        self, planning_session_id: str, **projection: Any
    ) -> PlanningSessionAggregate:
        """In-txn upsert on PK ``planning_session_id``, merging projection columns.

        Get-or-create on the session id, then overwrite each supplied projection
        column (``entity_counts`` / ``current_scores`` / the §2.4c telemetry folds /
        the ``last_processed_*`` cursors). Unsupplied columns keep their value;
        ``updated_at`` is auto-stamped by the model's ``onupdate``. Idempotent: the
        rollup re-running with the same fold rewrites the row in place. Runs inside
        the caller's transaction — no flush/commit here.
        """
        unknown = set(projection) - _AGGREGATE_PROJECTION_COLUMNS
        if unknown:
            raise ValueError(
                f"unknown planning aggregate projection columns: {sorted(unknown)}"
            )
        row = self.session.get(PlanningSessionAggregate, planning_session_id)
        if row is None:
            row = PlanningSessionAggregate(planning_session_id=planning_session_id)
            self.session.add(row)
        for column, value in projection.items():
            setattr(row, column, value)
        return row
