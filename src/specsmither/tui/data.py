"""The TUI's single read/mutate seam (planner/02 #2).

One object — :class:`Data` — binds the Observatory to the engine: it owns the
user-global DB engine + ``session_factory`` + the in-process :class:`Dispatcher`,
resolved from the workspace (``resolve_context``). Everything the screens render
flows through here; nothing in ``tui/`` imports ``operations`` / ``dispatch`` /
``lifecycle`` directly.

Three contracts the rest of ``tui/`` relies on:

* **Reads** return typed records / DTOs (snake_case), never raw wire dicts —
  record reads go through the per-transaction stores (``make_stores``), the
  graph/context reads through :mod:`specsmither.operations.queries`. No cache: a
  read always hits the DB, so it reflects the last in-txn recompute.
* **Two mutation paths.** Bootstrap / out-of-session edits call
  :mod:`specsmither.operations` directly (they *raise* :class:`CrudError`, caught
  and re-raised as :class:`TuiError` for the ``ErrorPanel``). In-session,
  phase-classified edits go through the :class:`Dispatcher`
  (``action_planning_session`` etc.), which never raises — a ``standard_error``
  envelope becomes a :class:`TuiError` (ErrorPanel) and a ``lifecycle`` envelope a
  :class:`LifecycleResult` (a toast: gate pass/fail, late-op rollback, denial
  guidance). :meth:`Data.has_active_session` decides the path.
* **Determinism.** A clock may be injected (forwarded to ``make_dispatcher`` for
  report determinism); the action log renders a derived 1-based sequence, never
  raw ULIDs / timestamps, so snapshots are stable.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.exc import IntegrityError

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.repositories import make_stores
from specsmither.dispatch.facade import make_dispatcher
from specsmither.domain.records import (
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
)
from specsmither.operations.errors import CrudError, to_crud_error
from specsmither.operations.queries import (
    ActionableResult,
    BlockedResult,
    CriticalPathResult,
    get_blocked_tickets,
    get_critical_path,
    get_dependency_tree,
    get_next_actionable_tickets,
)
from specsmither.operations.search import SearchResult, search_tickets
from specsmither.operations.workspace import (
    InitResult,
    WorkspaceConfig,
    init,
    resolve_context,
    resolve_workspace_root,
    write_workspace_config,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.db.repositories import ProjectRecord

__all__ = [
    "ActionRow",
    "ContextInfo",
    "Data",
    "DomainErr",
    "EpicTree",
    "LifecycleResult",
    "LogRow",
    "ProjectTree",
    "SessionView",
    "SpecTree",
    "TransitionRow",
    "TreeSnapshot",
    "TuiError",
]


# --------------------------------------------------------------------------- #
# error surface                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DomainErr:
    """A normalized domain error for the ``ErrorPanel`` (never parsed prose)."""

    code: str
    message: str
    next_actions: list[str]
    context: dict[str, Any] | None

    @classmethod
    def from_crud(cls, exc: CrudError) -> DomainErr:
        """From a raised :class:`CrudError` (the direct-operations path)."""
        return cls(
            code=exc.code.value,
            message=exc.message,
            next_actions=list(exc.next_actions or []),
            context=dict(exc.context) if exc.context else None,
        )

    @classmethod
    def from_envelope(cls, content: Mapping[str, Any]) -> DomainErr:
        """From a ``standard_error`` envelope (the dispatcher path)."""
        guidance = content.get("guidance") or {}
        prose = guidance.get("prose") if isinstance(guidance, Mapping) else None
        next_actions = guidance.get("next_actions") if isinstance(guidance, Mapping) else None
        message = content.get("message") or prose or "Operation failed."
        return cls(
            code=str(content.get("code", "INTERNAL")),
            message=str(message),
            next_actions=[str(a) for a in (next_actions or [])],
            context=dict(content.get("context") or {}) or None,
        )


class TuiError(Exception):
    """A domain error surfaced to the TUI's ``ErrorPanel`` (carries a :class:`DomainErr`)."""

    def __init__(self, err: DomainErr) -> None:
        super().__init__(err.message)
        self.err = err


# --------------------------------------------------------------------------- #
# lifecycle result (parsed agent_response)                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LifecycleResult:
    """A parsed planning ``agent_response`` (the in-session / handover outcome).

    ``outcome`` self-discriminates success vs ``"denied"`` (a denial rides the same
    lifecycle envelope, never a ``standard_error``); ``variant`` carries the UX
    classification (``phase_rollback`` for an accepted late op, ``denied`` for a
    blocked one, ``gate_passed`` / ``gate_failed`` …).
    """

    outcome: str
    session_id: str | None
    spec_id: str | None
    phase: str | None
    status: str | None
    variant: str | None
    guidance: str
    gate_result: str | None
    gate_passed: bool | None
    score: float | None
    score_global: float | None
    findings: list[dict[str, Any]]
    next_entities: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def denied(self) -> bool:
        return self.outcome == "denied"

    @property
    def rolled_back(self) -> bool:
        return self.variant == "phase_rollback"

    @classmethod
    def from_agent_response(cls, ar: Mapping[str, Any]) -> LifecycleResult:
        return cls(
            outcome=str(ar.get("outcome", "")),
            session_id=_opt_str(ar.get("session_id")),
            spec_id=_opt_str(ar.get("spec_id")),
            phase=_opt_str(ar.get("phase")),
            status=_opt_str(ar.get("status")),
            variant=_opt_str(ar.get("variant")),
            guidance=str(ar.get("guidance") or ""),
            gate_result=_opt_str(ar.get("gate_result")),
            gate_passed=_opt_bool(ar.get("gate_passed")),
            score=_opt_float(ar.get("score")),
            score_global=_opt_float(ar.get("score_global")),
            findings=[dict(f) for f in (ar.get("findings") or []) if isinstance(f, Mapping)],
            next_entities=[
                dict(e) for e in (ar.get("next_entities") or []) if isinstance(e, Mapping)
            ],
            raw=dict(ar),
        )


# --------------------------------------------------------------------------- #
# planning-session views (ORM rows snapshotted to frozen DTOs)                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionView:
    """A read snapshot of a ``PlanningSession`` row (detached-safe)."""

    id: str
    specification_id: str
    status: str
    current_phase: str
    last_gate_result: str | None
    last_score: float | None
    actions_count: int
    pending_human_feedback: dict[str, Any] | None


@dataclass(frozen=True)
class ActionRow:
    """A read snapshot of a ``planning_session_actions`` row."""

    id: str
    operation: str | None
    phase: str | None
    outcome: str | None
    actor: str | None
    guidance_variant: str | None
    findings_categories: list[str] | None
    score: float | None
    created_at: str


@dataclass(frozen=True)
class TransitionRow:
    """A read snapshot of a ``planning_phase_transitions`` row."""

    id: str
    from_phase: str | None
    to_phase: str | None
    trigger: str | None
    actor: str | None
    created_at: str


@dataclass(frozen=True)
class LogRow:
    """One merged action-log entry (newest-first), carrying its chronological seq."""

    seq: int
    kind: str  # "action" | "transition"
    action: ActionRow | None
    transition: TransitionRow | None


# --------------------------------------------------------------------------- #
# browse tree + context bar                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EpicTree:
    epic: EpicRecord
    tickets: list[TicketRecord]


@dataclass(frozen=True)
class SpecTree:
    spec: SpecificationRecord
    epics: list[EpicTree]


@dataclass(frozen=True)
class ProjectTree:
    project: ProjectRecord
    specs: list[SpecTree]


@dataclass(frozen=True)
class TreeSnapshot:
    """The whole project→spec→epic→ticket forest, read in one session (consistent)."""

    projects: list[ProjectTree]


@dataclass(frozen=True)
class ContextInfo:
    """What the always-visible ContextBar renders for the active selection."""

    project: ProjectRecord | None
    spec: SpecificationRecord | None
    session: SessionView | None
    ready: int
    blocked: int


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _opt_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- #
# the seam                                                                     #
# --------------------------------------------------------------------------- #


class Data:
    """The Observatory's read/mutate binding over one user-global DB."""

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        clock: datetime | None = None,
    ) -> None:
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._env = env
        self._clock = clock
        self.context = resolve_context(self.cwd, env)
        # init_db is create-or-migrate (idempotent): lazily creates the user-global
        # DB file so the TUI can read an empty engine before the workspace is bound.
        self._engine = init_db(self.context.db_path)
        self.session_factory: sessionmaker[Session] = make_session_factory(self._engine)
        self.dispatcher = make_dispatcher(
            self.session_factory, clock=clock, project_root=resolve_workspace_root(self.cwd)
        )

    # -- lifecycle ------------------------------------------------------------ #

    def close(self) -> None:
        """Dispose the engine (release the SQLite file handle)."""
        self._engine.dispose()

    # -- workspace / init ----------------------------------------------------- #

    @property
    def initialized(self) -> bool:
        """Has this workspace been bound to a project (``config.json`` written)?"""
        return self.context.project_id is not None

    def initialize_workspace(self, *, project_name: str | None = None) -> InitResult:
        """Run ``operations.workspace.init`` (idempotent) and refresh the context."""
        result = init(self.cwd, project_name=project_name, env=self._env)
        self.context = resolve_context(self.cwd, self._env)
        return result

    def bind_specification(self, specification_id: str | None) -> None:
        """Persist the active spec into the workspace ``config.json`` (best effort).

        The bound ``projectId`` follows the spec's OWN project so the binding stays
        internally consistent even when the active spec belongs to a different
        project than the one currently bound (the DB is user-global / multi-project).
        """
        project_id = self.context.project_id
        if specification_id is not None:
            with contextlib.suppress(CrudError):
                project_id = self.specification(specification_id).project_id
        if project_id is None:
            return
        write_workspace_config(
            self.cwd,
            WorkspaceConfig(project_id=project_id, specification_id=specification_id),
        )
        self.context = resolve_context(self.cwd, self._env)

    # -- reads: entities (records) ------------------------------------------- #

    def projects(self) -> list[ProjectRecord]:
        with self.session_factory() as session:
            return make_stores(session).projects.list_projects(user_id="local")

    def project(self, project_id: str) -> ProjectRecord:
        with self.session_factory() as session:
            return make_stores(session).projects.get_project(project_id)

    def specifications(self, project_id: str) -> list[SpecificationRecord]:
        with self.session_factory() as session:
            return make_stores(session).specifications.list_specifications(project_id=project_id)

    def specification(self, specification_id: str) -> SpecificationRecord:
        with self.session_factory() as session:
            return make_stores(session).specifications.get_specification(specification_id)

    def epics(self, specification_id: str) -> list[EpicRecord]:
        with self.session_factory() as session:
            return make_stores(session).epics.list_epics(specification_id=specification_id)

    def epic(self, epic_id: str) -> EpicRecord:
        with self.session_factory() as session:
            return make_stores(session).epics.get_epic(epic_id)

    def tickets(self, epic_id: str) -> list[TicketRecord]:
        with self.session_factory() as session:
            return make_stores(session).tickets.list_tickets(epic_id=epic_id)

    def ticket(self, ticket_id: str) -> TicketRecord:
        with self.session_factory() as session:
            record = make_stores(session).tickets.get_ticket(ticket_id)
        if record is None:
            raise TuiError(DomainErr("NOT_FOUND", f"Ticket not found: {ticket_id}", [], None))
        return record

    def tickets_for_spec(self, specification_id: str) -> list[TicketRecord]:
        with self.session_factory() as session:
            stores = make_stores(session)
            tickets: list[TicketRecord] = []
            for epic in stores.epics.list_epics(specification_id=specification_id):
                tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
        return tickets

    def dependencies(self, specification_id: str) -> list[DependencyEdge]:
        with self.session_factory() as session:
            return make_stores(session).ticket_dependencies.list_dependencies(
                specification_id=specification_id
            )

    def load_tree(self) -> TreeSnapshot:
        """The whole project→spec→epic→ticket forest, read in one consistent session."""
        with self.session_factory() as session:
            stores = make_stores(session)
            projects: list[ProjectTree] = []
            for project in stores.projects.list_projects(user_id="local"):
                specs: list[SpecTree] = []
                for spec in stores.specifications.list_specifications(project_id=project.id):
                    epics: list[EpicTree] = []
                    for epic in stores.epics.list_epics(specification_id=spec.id):
                        tickets = stores.tickets.list_tickets(epic_id=epic.id)
                        epics.append(EpicTree(epic=epic, tickets=tickets))
                    specs.append(SpecTree(spec=spec, epics=epics))
                projects.append(ProjectTree(project=project, specs=specs))
        return TreeSnapshot(projects=projects)

    # -- reads: DAG / context (graph DTOs from the materialized tree) --------- #

    def dependency_tree(self, specification_id: str) -> dict[str, Any] | None:
        """The cached dependency tree, or ``None`` if the spec was never recomputed."""
        try:
            return get_dependency_tree(self.session_factory, specification_id)
        except CrudError:
            return None

    def critical_path(self, specification_id: str) -> CriticalPathResult | None:
        try:
            return get_critical_path(self.session_factory, specification_id)
        except CrudError:
            return None

    def blocked(self, specification_id: str) -> BlockedResult | None:
        try:
            return get_blocked_tickets(self.session_factory, specification_id)
        except CrudError:
            return None

    def actionable(self, specification_id: str, *, limit: int = 50) -> ActionableResult | None:
        try:
            return get_next_actionable_tickets(self.session_factory, specification_id, limit=limit)
        except CrudError:
            return None

    # -- reads: planning session + action log -------------------------------- #

    def active_session(self, specification_id: str) -> SessionView | None:
        with self.session_factory() as session:
            row = make_stores(session).planning_sessions.get_active_for_spec(specification_id)
            if row is None:
                return None
            return SessionView(
                id=row.id,
                specification_id=row.specification_id,
                status=row.status,
                current_phase=row.current_phase,
                last_gate_result=row.last_gate_result,
                last_score=row.last_score,
                actions_count=row.actions_count,
                pending_human_feedback=row.pending_human_feedback,
            )

    def session_log(self, planning_session_id: str) -> list[LogRow]:
        """The action + transition audit, merged newest-first with a derived seq.

        Each stream is store-ordered (ULID id = chronological); we stable-merge by
        ``(created_at, kind_rank, per-stream index)`` so the order is deterministic
        and ULID-randomness-independent (ids/timestamps are never rendered).
        """
        with self.session_factory() as session:
            stores = make_stores(session)
            actions = [
                ActionRow(
                    id=a.id,
                    operation=a.operation,
                    phase=a.phase,
                    outcome=a.outcome,
                    actor=a.actor,
                    guidance_variant=a.guidance_variant,
                    findings_categories=a.findings_categories,
                    score=a.score,
                    created_at=a.created_at,
                )
                for a in stores.planning_actions.list_actions(planning_session_id)
            ]
            transitions = [
                TransitionRow(
                    id=t.id,
                    from_phase=t.from_phase,
                    to_phase=t.to_phase,
                    trigger=t.trigger,
                    actor=t.actor,
                    created_at=t.created_at,
                )
                for t in stores.planning_transitions.list_transitions(planning_session_id)
            ]
        # stable, deterministic merge: created_at asc, then actions (0) before
        # transitions (1), then per-stream chronological index.
        keyed: list[tuple[tuple[str, int, int], str, ActionRow | None, TransitionRow | None]] = []
        for i, a in enumerate(actions):
            keyed.append(((a.created_at, 0, i), "action", a, None))
        for i, t in enumerate(transitions):
            keyed.append(((t.created_at, 1, i), "transition", None, t))
        keyed.sort(key=lambda e: e[0])
        rows: list[LogRow] = [
            LogRow(seq=rank, kind=kind, action=action, transition=transition)
            for rank, (_key, kind, action, transition) in enumerate(keyed, start=1)
        ]
        rows.reverse()  # newest-first display
        return rows

    # -- reads: search + context bar ----------------------------------------- #

    def search(self, query: str, *, project_id: str) -> SearchResult:
        """Substring search over the active project's tickets (engine Python scoring)."""
        return search_tickets(self.session_factory, query=query, project_id=project_id)

    def context_info(
        self, *, project_id: str | None, specification_id: str | None
    ) -> ContextInfo:
        project = self.project(project_id) if project_id else None
        spec = self.specification(specification_id) if specification_id else None
        session = self.active_session(specification_id) if specification_id else None
        ready = blocked = 0
        if specification_id is not None:
            tree = self.dependency_tree(specification_id)
            if tree is not None:
                summary = tree.get("summary", {})
                ready = len(summary.get("ready_tickets", []) or [])
                blocked = len(summary.get("blocked_tickets", []) or [])
            elif spec is not None:
                ready = spec.ready_ticket_count
        return ContextInfo(
            project=project, spec=spec, session=session, ready=ready, blocked=blocked
        )

    # -- mutations: bootstrap / direct CRUD (raise CrudError -> TuiError) ----- #

    def has_active_session(self, specification_id: str) -> str | None:
        """The active planning session id for a spec (its presence selects path b)."""
        view = self.active_session(specification_id)
        return view.id if view is not None else None

    def spec_id_of_epic(self, epic_id: str) -> str:
        return self.epic(epic_id).specification_id

    def spec_id_of_ticket(self, ticket_id: str) -> str:
        return self.epic(self.ticket(ticket_id).epic_id).specification_id

    def create_project(self, name: str, description: str | None = None) -> ProjectRecord:
        from specsmither.operations.project import create_project

        with self._crud():
            return create_project(self.session_factory, name=name, description=description)

    def update_project(self, project_id: str, changes: Mapping[str, Any]) -> ProjectRecord:
        from specsmither.operations.project import update_project

        with self._crud():
            return update_project(self.session_factory, project_id, changes)

    def delete_project(self, project_id: str) -> None:
        from specsmither.operations.project import delete_project

        with self._crud():
            delete_project(self.session_factory, project_id)

    def create_specification(
        self, project_id: str, title: str, *, description: str | None = None
    ) -> SpecificationRecord:
        from specsmither.operations.crud import create_specification

        fields = {"description": description} if description else None
        with self._crud():
            return create_specification(
                self.session_factory, project_id=project_id, title=title, fields=fields
            )

    def update_specification(
        self, specification_id: str, changes: Mapping[str, Any]
    ) -> SpecificationRecord:
        from specsmither.operations.crud import update_specification

        with self._crud():
            return update_specification(self.session_factory, specification_id, changes)

    def delete_specification(self, specification_id: str) -> None:
        from specsmither.operations.crud import delete_specification

        with self._crud():
            delete_specification(self.session_factory, specification_id)

    def create_epic(
        self, specification_id: str, *, title: str, description: str = "", objective: str = ""
    ) -> EpicRecord:
        from specsmither.operations.crud import create_epic

        with self._crud():
            return create_epic(
                self.session_factory,
                specification_id=specification_id,
                title=title,
                description=description,
                objective=objective,
            )

    def update_epic(self, epic_id: str, changes: Mapping[str, Any]) -> EpicRecord:
        from specsmither.operations.crud import update_epic

        with self._crud():
            return update_epic(self.session_factory, epic_id, changes)

    def delete_epic(self, epic_id: str) -> None:
        from specsmither.operations.crud import delete_epic

        with self._crud():
            delete_epic(self.session_factory, epic_id)

    def create_ticket(
        self, epic_id: str, *, title: str, fields: Mapping[str, Any] | None = None
    ) -> TicketRecord:
        from specsmither.operations.crud import create_ticket

        with self._crud():
            return create_ticket(self.session_factory, epic_id=epic_id, title=title, fields=fields)

    def update_ticket(self, ticket_id: str, changes: Mapping[str, Any]) -> TicketRecord:
        from specsmither.operations.crud import update_ticket

        with self._crud():
            return update_ticket(self.session_factory, ticket_id, changes)

    def delete_ticket(self, ticket_id: str) -> None:
        from specsmither.operations.crud import delete_ticket

        with self._crud():
            delete_ticket(self.session_factory, ticket_id)

    def add_dependency(self, ticket_id: str, depends_on_id: str) -> None:
        from specsmither.operations.crud import add_dependency

        with self._crud():
            add_dependency(self.session_factory, ticket_id, depends_on_id)

    def remove_dependency(self, ticket_id: str, depends_on_id: str) -> None:
        from specsmither.operations.crud import remove_dependency

        with self._crud():
            remove_dependency(
                self.session_factory, ticket_id=ticket_id, depends_on_id=depends_on_id
            )

    # -- mutations: planning lifecycle (via dispatcher; never raises) --------- #

    def start_session(self, specification_id: str, *, user_id: str = "human") -> LifecycleResult:
        return self._lifecycle(
            "start_planning_session", {"specId": specification_id, "userId": user_id}
        )

    def complete_session(self, session_id: str, *, user_id: str = "human") -> LifecycleResult:
        return self._lifecycle(
            "complete_planning_session", {"sessionId": session_id, "userId": user_id}
        )

    def approve(self, session_id: str, *, user_id: str = "human") -> LifecycleResult:
        return self._lifecycle("approve_handover", {"sessionId": session_id, "userId": user_id})

    def reject(
        self, session_id: str, *, feedback: str | None = None, user_id: str = "human"
    ) -> LifecycleResult:
        if feedback and feedback.strip():
            return self._lifecycle(
                "reject_handover_with_feedback",
                {"sessionId": session_id, "feedback": feedback, "userId": user_id},
            )
        return self._lifecycle("reject_handover", {"sessionId": session_id, "userId": user_id})

    def action(
        self,
        session_id: str,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "human",
    ) -> LifecycleResult:
        """Run ONE phase-classified planning op (native applies / late rewinds / forbidden denies)."""
        args: dict[str, Any] = {"sessionId": session_id, "operation": operation, "actor": actor}
        if payload is not None:
            args["payload"] = dict(payload)
        return self._lifecycle("action_planning_session", args)

    def poll_status(self, session_id: str, *, actor: str = "human") -> LifecycleResult:
        """Read-only status poll (delivers + clears one-shot human feedback once)."""
        return self.action(session_id, "get_planning_status", actor=actor)

    # -- internals ----------------------------------------------------------- #

    def _lifecycle(self, tool: str, args: Mapping[str, Any]) -> LifecycleResult:
        content = self.dispatcher.dispatch(tool, args)
        kind = content.get("kind")
        if kind == "standard_error":
            raise TuiError(DomainErr.from_envelope(content))
        if kind == "lifecycle":
            agent_response = content.get("agent_response") or {}
            return LifecycleResult.from_agent_response(agent_response)
        raise TuiError(
            DomainErr("INTERNAL", f"Unexpected dispatch response for {tool}.", [], None)
        )

    def _crud(self) -> _CrudGuard:
        return _CrudGuard()


class _CrudGuard:
    """``with self._crud():`` — translates a raised :class:`CrudError` to :class:`TuiError`."""

    def __enter__(self) -> _CrudGuard:
        return self

    def __exit__(
        self, exc_type: object, exc: BaseException | None, tb: object
    ) -> Literal[False]:
        if isinstance(exc, CrudError):
            raise TuiError(DomainErr.from_crud(exc)) from exc
        if isinstance(exc, IntegrityError):
            # A raw persistence failure that slipped past the store's own bridge —
            # classify it so it still lands in the ErrorPanel, never the protocol.
            raise TuiError(DomainErr.from_crud(to_crud_error(exc))) from exc
        return False


def make_data(
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    clock: datetime | None = None,
) -> Data:
    """Construct the :class:`Data` seam (the app's single binding-point factory)."""
    return Data(cwd=cwd, env=env, clock=clock)
