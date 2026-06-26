"""``*StoreSqlite`` repositories implementing the store interfaces (the L5 layer).

The third store implementation set (after the TS ``appsync-impl`` / ``json-impl``),
drop-in via the same DI bag (:class:`AllStores` / :func:`make_stores`). Tickets
decompose-on-write / recompose-on-read (explicit replace-all). Planning session /
action / transition stores are read-only (writes flow through the WritePlan
executor); the datapoint + aggregate stores carry the idempotent in-txn upsert the
rollup uses. Every store is session-bound and never owns the transaction; count
columns are written ONLY by the recompute worklist (#14) — the ``updateX*Count``
delta-mutators are dropped (no-ops).

This package's public surface re-exports the shared base, every concrete store
class, the :class:`ProjectRecord` DTO defined alongside the core stores, and the
per-transaction DI bag.
"""

from __future__ import annotations

from specsmither.db.repositories.all_stores import AllStores, make_stores
from specsmither.db.repositories.base import SessionStore, require_found
from specsmither.db.repositories.core_stores import (
    BlueprintStoreSqlite,
    EpicStoreSqlite,
    ProjectRecord,
    ProjectStoreSqlite,
    SpecStoreSqlite,
    TicketDependencyStoreSqlite,
)
from specsmither.db.repositories.planning_stores import (
    PlanningEntityScoreDatapointStore,
    PlanningPhaseTransitionStore,
    PlanningSessionActionStore,
    PlanningSessionAggregateStore,
    PlanningSessionStore,
)
from specsmither.db.repositories.ticket_store import TicketStoreSqlite
from specsmither.db.repositories.work_stores import (
    WorkSessionAcceptanceCheckStore,
    WorkSessionFileChangeStore,
    WorkSessionImplStepCompletionStore,
    WorkSessionStore,
    WorkSessionTestResultStore,
)

__all__ = [
    "AllStores",
    "BlueprintStoreSqlite",
    "EpicStoreSqlite",
    "PlanningEntityScoreDatapointStore",
    "PlanningPhaseTransitionStore",
    "PlanningSessionActionStore",
    "PlanningSessionAggregateStore",
    "PlanningSessionStore",
    "ProjectRecord",
    "ProjectStoreSqlite",
    "SessionStore",
    "SpecStoreSqlite",
    "TicketDependencyStoreSqlite",
    "TicketStoreSqlite",
    "WorkSessionAcceptanceCheckStore",
    "WorkSessionFileChangeStore",
    "WorkSessionImplStepCompletionStore",
    "WorkSessionStore",
    "WorkSessionTestResultStore",
    "make_stores",
    "require_found",
]
