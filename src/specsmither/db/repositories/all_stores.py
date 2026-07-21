"""The per-transaction DI bag: one ``Session`` → every ``*StoreSqlite`` repository.

:class:`AllStores` is the frozen container the WritePlan executor / CRUD primitive /
lifecycle binds **once per transaction**. Given the caller's
:class:`~sqlalchemy.orm.Session`, :func:`make_stores` constructs every store over
that same session, so all of them share one unit of work (and therefore one
``BEGIN IMMEDIATE`` write lock). The executor opens the transaction, threads this
bag through the verbs, runs the recompute worklist, and commits — the stores
themselves never own the transaction boundary (architecture §4, invariant 3).

The bag is the store DI object: the attribute surface the operations layer expects,
bound to the SQLite implementations. The attribute names are the stable binding
contract (L6/L7 depend on them):

* entity stores — ``tickets`` / ``projects`` / ``specifications`` / ``epics`` /
  ``blueprints`` / ``ticket_dependencies``
* work-session stores — ``work_sessions`` + the four gate dimensions
  (``work_acceptance_checks`` / ``work_step_completions`` / ``work_file_changes`` /
  ``work_test_results``)
* planning stores — ``planning_sessions`` / ``planning_actions`` /
  ``planning_transitions`` / ``planning_datapoints`` / ``planning_aggregates``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from specsmither.db.repositories.core_stores import (
    BlueprintStoreSqlite,
    EpicStoreSqlite,
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

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = ["AllStores", "make_stores"]


@dataclass(frozen=True)
class AllStores:
    """Immutable DI bag exposing every ``*StoreSqlite`` over one shared ``Session``.

    Frozen: the bag is rebuilt per transaction (cheap — stores hold only the
    session), never mutated. ``session`` is exposed so a caller that already holds
    the bag can reach the unit of work (e.g. to run the recompute worklist) without
    threading the session separately.
    """

    session: Session
    tickets: TicketStoreSqlite
    projects: ProjectStoreSqlite
    specifications: SpecStoreSqlite
    epics: EpicStoreSqlite
    blueprints: BlueprintStoreSqlite
    ticket_dependencies: TicketDependencyStoreSqlite
    work_sessions: WorkSessionStore
    work_acceptance_checks: WorkSessionAcceptanceCheckStore
    work_step_completions: WorkSessionImplStepCompletionStore
    work_file_changes: WorkSessionFileChangeStore
    work_test_results: WorkSessionTestResultStore
    planning_sessions: PlanningSessionStore
    planning_actions: PlanningSessionActionStore
    planning_transitions: PlanningPhaseTransitionStore
    planning_datapoints: PlanningEntityScoreDatapointStore
    planning_aggregates: PlanningSessionAggregateStore


def make_stores(session: Session) -> AllStores:
    """Construct the full :class:`AllStores` bag over *session* (every store shares it)."""
    return AllStores(
        session=session,
        tickets=TicketStoreSqlite(session),
        projects=ProjectStoreSqlite(session),
        specifications=SpecStoreSqlite(session),
        epics=EpicStoreSqlite(session),
        blueprints=BlueprintStoreSqlite(session),
        ticket_dependencies=TicketDependencyStoreSqlite(session),
        work_sessions=WorkSessionStore(session),
        work_acceptance_checks=WorkSessionAcceptanceCheckStore(session),
        work_step_completions=WorkSessionImplStepCompletionStore(session),
        work_file_changes=WorkSessionFileChangeStore(session),
        work_test_results=WorkSessionTestResultStore(session),
        planning_sessions=PlanningSessionStore(session),
        planning_actions=PlanningSessionActionStore(session),
        planning_transitions=PlanningPhaseTransitionStore(session),
        planning_datapoints=PlanningEntityScoreDatapointStore(session),
        planning_aggregates=PlanningSessionAggregateStore(session),
    )
