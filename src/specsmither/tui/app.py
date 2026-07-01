"""The Observatory app shell (planner/02 #1, #8–#10): tabs, the live ContextBar,
live refresh, and every interactive action.

One ``Data`` seam, shared with the agent through the same user-global DB. Mutations
follow two paths (chosen by :meth:`Data.has_active_session`): out-of-session edits
go DIRECT to ``operations`` (raise → ErrorPanel); in-session edits route through the
phase-classified ``action_planning_session`` (native applies / late rewinds /
forbidden denies → a toast). Every mutation is followed by a full refresh.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.reactive import reactive
from textual.widgets import Button, DataTable, Footer, Header, Input, TabbedContent, TabPane, Tree

from specsmither.db.base import new_ulid
from specsmither.tui.data import Data, LifecycleResult, TuiError, make_data
from specsmither.tui.screens import DagView, PlanningMonitor, SearchView, SpecBrowser, WorkProgress
from specsmither.tui.widgets import (
    ConfirmModal,
    ContextBar,
    ErrorPanel,
    FeedbackModal,
    InitModal,
    SpecEpicEditModal,
    TicketEditModal,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: Widgets that follow the active spec (refreshed on a spec switch — NOT the tree).
#: Type selectors (one of each) — the ``#dag``/``#work`` ids are the TabPanes, not
#: the screens, so reload/focus must target the widget classes.
_FOLLOWER_SELECTORS = ("#ctxbar", "DagView", "PlanningMonitor", "WorkProgress")
_ALL_SELECTORS = (
    "#ctxbar",
    "SpecBrowser",
    "DagView",
    "PlanningMonitor",
    "WorkProgress",
    "SearchView",
)


class Observatory(App[None]):
    """The local SpecSmither Observatory — the only human surface."""

    CSS_PATH = "app.tcss"
    TITLE = "SpecSmither"
    SUB_TITLE = "local Observatory"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("c", "create", "New", show=True),
        Binding("e", "edit", "Edit", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("n", "advance", "Advance", show=True),
        Binding("a", "approve", "Approve", show=True),
        Binding("j", "reject", "Reject", show=True),
        Binding("i", "init", "Init", show=True),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("1", "show_tab('browser')", "Browser", show=False),
        Binding("2", "show_tab('dag')", "DAG", show=False),
        Binding("3", "show_tab('planning')", "Planning", show=False),
        Binding("4", "show_tab('work')", "Work", show=False),
        Binding("5", "show_tab('search')", "Search", show=False),
        Binding("slash", "focus_search", "Find", show=False),
    ]

    current_spec_id: reactive[str | None] = reactive(None, init=False)

    def __init__(
        self,
        data: Data | None = None,
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        clock: datetime | None = None,
    ) -> None:
        super().__init__()
        self.data = data if data is not None else make_data(cwd=cwd, env=env, clock=clock)
        self.active_project_id: str | None = None

    # -- compose / mount ------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ContextBar(id="ctxbar")
        with TabbedContent(initial="browser"):
            with TabPane("Browser", id="browser"):
                yield SpecBrowser()
            with TabPane("DAG", id="dag"):
                yield DagView()
            with TabPane("Planning", id="planning"):
                yield PlanningMonitor()
            with TabPane("Work", id="work"):
                yield WorkProgress()
            with TabPane("Search", id="search"):
                yield SearchView()
        yield Footer()

    def on_mount(self) -> None:
        self.active_project_id = self._resolve_active_project()
        self.current_spec_id = self._resolve_active_spec()
        self.reload_all()
        self.call_after_refresh(self._focus_tab, "browser")
        if not self.data.initialized:
            self.call_after_refresh(self._hint_init)

    def _resolve_active_project(self) -> str | None:
        if self.data.context.project_id is not None:
            return self.data.context.project_id
        projects = self.data.projects()
        return projects[0].id if projects else None

    def _resolve_active_spec(self) -> str | None:
        if self.data.context.specification_id is not None:
            return self.data.context.specification_id
        if self.active_project_id is None:
            return None
        specs = self.data.specifications(self.active_project_id)
        return specs[0].id if specs else None

    def _hint_init(self) -> None:
        self.notify(
            "Workspace not initialized — press i to scaffold (init).",
            severity="warning",
            timeout=6,
        )

    # -- live refresh --------------------------------------------------------- #

    def _reload(self, selectors: tuple[str, ...]) -> None:
        for selector in selectors:
            try:
                widget = self.query_one(selector)
            except Exception:  # pragma: no cover - a not-yet-mounted pane
                continue
            reload = getattr(widget, "reload", None)
            if callable(reload):
                with contextlib.suppress(Exception):  # pragma: no cover - laggy view
                    reload()

    def reload_all(self) -> None:
        self._reload(_ALL_SELECTORS)

    def _reload_followers(self) -> None:
        self._reload(_FOLLOWER_SELECTORS)

    def watch_current_spec_id(self, _old: str | None, _new: str | None) -> None:
        # A viewport change for the follower tabs — do NOT rebuild the tree (it would
        # reset the cursor mid-navigation).
        self._reload_followers()

    def set_current_spec(self, spec_id: str | None) -> None:
        """Commit the active spec (followers refresh; persist into the workspace binding).

        Also re-points ``active_project_id`` at the spec's OWN project so the
        ContextBar, Search scope, and the workspace binding stay consistent when the
        user navigates into a spec under a different project (the DB is multi-project).
        """
        self.current_spec_id = spec_id
        if spec_id is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort
                self.active_project_id = self.data.specification(spec_id).project_id
        with contextlib.suppress(Exception):  # pragma: no cover - binding is best-effort
            self.data.bind_specification(spec_id)

    # -- init ----------------------------------------------------------------- #

    def action_init(self) -> None:
        self.push_screen(InitModal())

    def run_init(self) -> tuple[bool, list[tuple[str, str, str]]]:
        """Bootstrap the workspace + scaffold ``.claude`` (called by the InitModal)."""
        from specsmither.tui import scaffold

        result = self.data.initialize_workspace()
        emitted = scaffold.emit(self.data.cwd)
        self.active_project_id = self.data.context.project_id or self.active_project_id
        if self.current_spec_id is None:
            self.current_spec_id = self._resolve_active_spec()
        self.reload_all()
        return (not result.created_project, emitted)

    # -- tab navigation ------------------------------------------------------- #

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id
        self._focus_tab(tab_id)

    def action_focus_search(self) -> None:
        self.query_one(TabbedContent).active = "search"
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self.query_one("#search-input", Input).focus()

    def _focus_tab(self, tab_id: str) -> None:
        """Move focus INTO the active pane (Textual reverts a programmatic switch
        unless focus leaves the now-hidden source pane); never focus a text Input."""
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            if tab_id == "browser":
                self.query_one(SpecBrowser).query_one(Tree).focus()
            elif tab_id == "dag":
                self.query_one(DagView).focus()
            elif tab_id == "work":
                self.query_one(WorkProgress).focus()
            elif tab_id == "planning":
                self.query_one("#pm-advance", Button).focus()
            elif tab_id == "search":
                # focus the results table, NOT the Input (an Input traps the 1-5 nav).
                self.query_one("#search-results", DataTable).focus()

    def action_refresh(self) -> None:
        self.reload_all()
        self.notify("Refreshed.")

    # -- planning / handover -------------------------------------------------- #

    def action_advance(self) -> None:
        spec_id = self.current_spec_id
        if spec_id is None:
            self.notify("Select a spec in the Browser first.", severity="warning")
            return

        def go() -> None:
            session_id = self.data.has_active_session(spec_id)
            if session_id is None:
                self._announce(self.data.start_session(spec_id), started=True)
            else:
                self._announce(self.data.complete_session(session_id))

        self._guard(go)
        self.reload_all()

    def action_approve(self) -> None:
        spec_id = self.current_spec_id
        session = self.data.active_session(spec_id) if spec_id else None
        if session is None:
            self.notify("No active planning session.", severity="warning")
            return
        self._guard(lambda: self._announce(self.data.approve(session.id)))
        self.reload_all()

    def action_reject(self) -> None:
        spec_id = self.current_spec_id
        session = self.data.active_session(spec_id) if spec_id else None
        if session is None or session.status != "awaiting_human_review":
            self.notify("No session awaiting human review.", severity="warning")
            return

        def done(feedback: str | None) -> None:
            if feedback is None:
                return
            self._guard(lambda: self.data.reject(session.id, feedback=feedback))
            self.reload_all()
            self.notify("Handover rejected — returned to the agent.", severity="warning")

        self.push_screen(FeedbackModal(), done)

    def _announce(self, result: LifecycleResult, *, started: bool = False) -> None:
        if started:
            self.notify(f"Started planning · phase {result.phase}.")
            return
        if result.denied:
            self.notify(result.guidance or "Operation denied.", title="denied", severity="warning", timeout=7)
            return
        if result.rolled_back:
            self.notify(
                f"late op → session rolled back to {result.phase} (press n to re-validate)",
                severity="warning",
                timeout=7,
            )
            return
        if result.status == "closed":
            self.notify("Handover approved — spec is now ready.", timeout=6)
            return
        if result.status == "awaiting_human_review":
            gate = "PASS" if result.gate_result == "pass" else result.gate_result or "?"
            self.notify(f"gate {gate} · parked for review @ {result.phase} (press a to approve)")
            return
        gate = result.gate_result or "—"
        self.notify(f"applied · gate {gate} · phase {result.phase}")

    # -- cross-screen navigation --------------------------------------------- #

    def jump_to_ticket(self, ticket_id: str) -> None:
        try:
            spec_id = self.data.spec_id_of_ticket(ticket_id)
        except TuiError:
            return
        self.set_current_spec(spec_id)
        self.query_one(TabbedContent).active = "browser"
        self.query_one("#browser SpecBrowser", SpecBrowser).select_ticket(ticket_id)
        self._focus_tab("browser")

    # -- selection helpers ---------------------------------------------------- #

    def _browser(self) -> SpecBrowser:
        return self.query_one("#browser SpecBrowser", SpecBrowser)

    def _selection(self) -> tuple[str, str] | None:
        return self._browser().selected_target()

    def _spec_id_of(self, kind: str, ident: str) -> str:
        if kind == "spec":
            return ident
        if kind == "epic":
            return self.data.spec_id_of_epic(ident)
        return self.data.spec_id_of_ticket(ident)

    # -- create (c) ----------------------------------------------------------- #

    def action_create(self) -> None:
        sel = self._selection()
        kind = sel[0] if sel else None
        if kind in (None, "root"):
            self._create_project()
        elif kind == "project" and sel is not None:
            self._create_spec(sel[1])
        elif kind == "spec" and sel is not None:
            self._create_epic(sel[1])
        elif sel is not None:  # epic or ticket → a new ticket in that epic
            epic_id = sel[1] if kind == "epic" else self.data.ticket(sel[1]).epic_id
            self._create_ticket(epic_id)

    def _create_project(self) -> None:
        def done(form: dict[str, str] | None) -> None:
            if form is None or not form["title"].strip():
                return

            def go() -> None:
                project = self.data.create_project(form["title"], description=form["description"] or None)
                self.active_project_id = project.id
                self._browser().select_entity("project", project.id)
                self.notify(f"created project · {project.name}")

            self._guard(go)
            self.reload_all()

        self.push_screen(SpecEpicEditModal(heading="New project  (title = project name)"), done)

    def _create_spec(self, project_id: str) -> None:
        def done(form: dict[str, str] | None) -> None:
            if form is None or not form["title"].strip():
                return

            def go() -> None:
                spec = self.data.create_specification(
                    project_id, form["title"], description=form["description"] or None
                )
                self.set_current_spec(spec.id)
                self._browser().select_entity("spec", spec.id)
                self.notify(f"created spec · {spec.title} (draft) — press n to start planning")

            self._guard(go)
            self.reload_all()

        self.push_screen(SpecEpicEditModal(heading=f"New spec in {project_id[:8]}"), done)

    def _create_epic(self, spec_id: str) -> None:
        def done(form: dict[str, str] | None) -> None:
            if form is None or not form["title"].strip():
                return

            def go() -> None:
                session_id = self.data.has_active_session(spec_id)
                if session_id is not None:
                    self._announce(
                        self.data.action(
                            session_id,
                            "create_epic",
                            {"title": form["title"], "description": form["description"], "objective": ""},
                        )
                    )
                else:
                    epic = self.data.create_epic(spec_id, title=form["title"], description=form["description"])
                    self._browser().select_entity("epic", epic.id)
                    self.notify(f"created epic · {epic.title}")

            self._guard(go)
            self.reload_all()

        self.push_screen(SpecEpicEditModal(heading="New epic"), done)

    def _create_ticket(self, epic_id: str) -> None:
        spec_id = self.data.spec_id_of_epic(epic_id)
        # The in-session create_ticket op only persists title/description (AC / steps /
        # files / deps are authored later, in their own phases), so don't present those
        # fields when a session is active — they would be silently dropped otherwise.
        in_session = self.data.has_active_session(spec_id) is not None

        def done(form: dict[str, Any] | None) -> None:
            if form is None or not str(form["title"]).strip():
                return

            def go() -> None:
                session_id = self.data.has_active_session(spec_id)
                if session_id is not None:
                    self._announce(
                        self.data.action(
                            session_id,
                            "create_ticket",
                            {"epicId": epic_id, "title": form["title"], "description": form["description"]},
                        )
                    )
                else:
                    ticket = self.data.create_ticket(
                        epic_id, title=form["title"], fields=self._ticket_fields_direct(form)
                    )
                    self._apply_deps(spec_id, ticket.id, form.get("depends_on", []), session_id=None)
                    self._browser().select_entity("ticket", ticket.id)
                    self.notify(f"created ticket · {ticket.title}")

            self._guard(go)
            self.reload_all()

        self.push_screen(
            TicketEditModal(
                heading=f"New ticket in {epic_id[:8]}",
                show_status=False,
                show_planning_fields=not in_session,
            ),
            done,
        )

    # -- edit (e) ------------------------------------------------------------- #

    def action_edit(self) -> None:
        sel = self._selection()
        if sel is None or sel[0] not in ("spec", "epic", "ticket"):
            self.notify("Select a spec, epic, or ticket in the Browser first (then e).", severity="warning")
            return
        kind, ident = sel
        spec_id = self._spec_id_of(kind, ident)
        session_id = self.data.has_active_session(spec_id)
        if kind == "ticket":
            self._edit_ticket(ident, spec_id, session_id)
        elif kind == "epic":
            self._edit_epic(ident, spec_id, session_id)
        else:
            self._edit_spec(ident, session_id)

    def _edit_spec(self, spec_id: str, session_id: str | None) -> None:
        spec = self.data.specification(spec_id)

        def done(form: dict[str, str] | None) -> None:
            if form is None:
                return

            def go() -> None:
                if session_id is not None:
                    self._announce(
                        self.data.action(
                            session_id,
                            "update_spec",
                            {"fields": {"title": form["title"], "description": form["description"]}},
                        )
                    )
                else:
                    self.data.update_specification(
                        spec_id, {"title": form["title"], "description": form["description"]}
                    )
                    self.notify("update_spec applied.")

            self._guard(go)
            self.reload_all()

        self.push_screen(
            SpecEpicEditModal(heading=f"Edit spec {spec_id[:8]}", title=spec.title, description=spec.description or ""),
            done,
        )

    def _edit_epic(self, epic_id: str, spec_id: str, session_id: str | None) -> None:
        epic = self.data.epic(epic_id)

        def done(form: dict[str, str] | None) -> None:
            if form is None:
                return

            def go() -> None:
                if session_id is not None:
                    self._announce(
                        self.data.action(
                            session_id,
                            "update_epic",
                            {"id": epic_id, "fields": {"title": form["title"], "description": form["description"]}},
                        )
                    )
                else:
                    self.data.update_epic(epic_id, {"title": form["title"], "description": form["description"]})
                    self.notify("update_epic applied.")

            self._guard(go)
            self.reload_all()

        self.push_screen(
            SpecEpicEditModal(heading=f"Edit epic {epic_id[:8]}", title=epic.title, description=epic.description),
            done,
        )

    def _edit_ticket(self, ticket_id: str, spec_id: str, session_id: str | None) -> None:
        ticket = self.data.ticket(ticket_id)
        ac_lines = [f"{c.given} | {c.when} | {c.then}" for c in ticket.acceptance_criteria]
        step_lines = [s.text for s in ticket.implementation_steps]
        dep_labels = self._dep_labels(spec_id, ticket_id)

        def done(form: dict[str, Any] | None) -> None:
            if form is None:
                return

            def go() -> None:
                if session_id is not None:
                    self._announce(
                        self.data.action(
                            session_id,
                            "update_ticket",
                            {"id": ticket_id, "fields": self._ticket_fields_aps(form, full=True)},
                        )
                    )
                else:
                    changes = self._ticket_fields_direct(form, full=True)
                    changes["title"] = form["title"]
                    if "status" in form:
                        changes["status"] = form["status"]
                    self.data.update_ticket(ticket_id, changes)
                    self.notify("update_ticket applied.")
                self._apply_deps(spec_id, ticket_id, form.get("depends_on", []), session_id=session_id)

            self._guard(go)
            self.reload_all()

        self.push_screen(
            TicketEditModal(
                heading=f"Edit ticket {ticket_id[:8]}",
                title=ticket.title,
                description=ticket.description or "",
                status=ticket.status.value,
                acceptance=ac_lines,
                steps=step_lines,
                files=list(ticket.files_to_be_created),
                depends_on=dep_labels,
                show_status=session_id is None,
            ),
            done,
        )

    # -- delete (d) ----------------------------------------------------------- #

    def action_delete(self) -> None:
        sel = self._selection()
        kind = sel[0] if sel else None
        if kind == "ticket" and sel is not None:
            self._delete_ticket(sel[1])
        elif kind == "spec" and sel is not None:
            self._delete_spec(sel[1])
        else:
            self.notify("Select a ticket or a spec to delete, then d.", severity="warning")

    def _delete_ticket(self, ticket_id: str) -> None:
        spec_id = self.data.spec_id_of_ticket(ticket_id)
        session_id = self.data.has_active_session(spec_id)

        def done(yes: bool | None) -> None:
            if not yes:
                return

            def go() -> None:
                if session_id is not None:
                    self._announce(self.data.action(session_id, "delete_ticket", {"id": ticket_id}))
                else:
                    self.data.delete_ticket(ticket_id)
                    self.notify("delete_ticket applied.")
                self._browser().select_entity("spec", spec_id)

            self._guard(go)
            self.reload_all()

        self.push_screen(
            ConfirmModal(f"Delete ticket {ticket_id[:8]}?  (also removes dependencies on it)", confirm_label="Delete"),
            done,
        )

    def _delete_spec(self, spec_id: str) -> None:
        spec = self.data.specification(spec_id)

        def done(yes: bool | None) -> None:
            if not yes:
                return

            def go() -> None:
                self.data.delete_specification(spec_id)
                self.notify(f"deleted spec · {spec.title}")
                remaining = (
                    self.data.specifications(self.active_project_id) if self.active_project_id else []
                )
                self.set_current_spec(remaining[0].id if remaining else None)

            self._guard(go)
            self.reload_all()

        self.push_screen(
            ConfirmModal(
                f"Delete spec {spec_id[:8]} · {spec.title}?  (removes its epics/tickets; ends any active session)",
                confirm_label="Delete",
            ),
            done,
        )

    # -- ticket field builders + dependency diff ------------------------------ #

    @staticmethod
    def _parse_ac(line: str) -> tuple[str, str, str]:
        parts = [p.strip() for p in line.split("|")]
        given = parts[0] if parts else ""
        when = parts[1] if len(parts) > 1 else ""
        then = parts[2] if len(parts) > 2 else ""
        return given, when, then

    def _ticket_fields_direct(self, form: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        """Snake_case record fields for the DIRECT ``operations.crud`` path.

        ``full=True`` (edit) emits each child array even when empty, so clearing the
        last AC / step / file REPLACES-ALL with nothing; ``full=False`` (create) omits
        empty arrays (a fresh ticket simply has no rows).
        """
        fields: dict[str, Any] = {"description": form["description"]}
        ac = form.get("acceptance_criteria") or []
        if ac or full:
            fields["acceptance_criteria"] = [
                {"id": new_ulid(), "given": g, "when": w, "then": t, "order": i + 1}
                for i, (g, w, t) in enumerate(self._parse_ac(line) for line in ac)
            ]
        steps = form.get("steps") or []
        if steps or full:
            fields["implementation_steps"] = [
                {"id": new_ulid(), "text": text, "order": i + 1} for i, text in enumerate(steps)
            ]
        files = form.get("files") or []
        if files or full:
            fields["files_to_be_created"] = list(files)
        return fields

    def _ticket_fields_aps(self, form: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        """CamelCase WritePlan fields for the in-session ``action_planning_session`` path.

        ``full=True`` (edit) emits each child array even when empty (the WritePlan
        decompose is REPLACE-ALL), so an emptied field clears its rows.
        """
        fields: dict[str, Any] = {"title": form["title"], "description": form["description"]}
        ac = form.get("acceptance_criteria") or []
        if ac or full:
            fields["acceptanceCriteria"] = [
                {"given": g, "when": w, "then": t}
                for g, w, t in (self._parse_ac(line) for line in ac)
            ]
        steps = form.get("steps") or []
        if steps or full:
            fields["implementationSteps"] = list(steps)
        files = form.get("files") or []
        if files or full:
            fields["filesToBeCreated"] = list(files)
        return fields

    def _label_maps(self, spec_id: str) -> tuple[dict[str, str], dict[str, str]]:
        """``(label→id, id→label)`` for a spec's tickets (label = ``E{e}-T{t}``)."""
        label_to_id: dict[str, str] = {}
        id_to_label: dict[str, str] = {}
        for epic in self.data.epics(spec_id):
            for ticket in self.data.tickets(epic.id):
                num = ticket.ticket_number
                label = f"E{epic.epic_number}-T{num}" if epic.epic_number and num else ticket.id[:8]
                label_to_id[label] = ticket.id
                id_to_label[ticket.id] = label
        return label_to_id, id_to_label

    def _dep_labels(self, spec_id: str, ticket_id: str) -> list[str]:
        _, id_to_label = self._label_maps(spec_id)
        return [
            id_to_label.get(edge.depends_on_id, edge.depends_on_id[:8])
            for edge in self.data.dependencies(spec_id)
            if edge.ticket_id == ticket_id
        ]

    def _apply_deps(
        self, spec_id: str, ticket_id: str, wanted_labels: list[str], *, session_id: str | None
    ) -> None:
        """Diff the ticket's deps against the form labels; add/remove via the chosen path."""
        label_to_id, _ = self._label_maps(spec_id)
        # depends_on_id -> the REAL stored edge id. NEVER reconstruct it: a DIRECT-CRUD
        # edge carries a random ULID id (IdMixin), while an APS edge carries the
        # deterministic f"{from}--requires--{to}" — so deleting by a reconstructed id
        # would silently miss draft-authored edges.
        current_ids: dict[str, str] = {
            edge.depends_on_id: edge.id
            for edge in self.data.dependencies(spec_id)
            if edge.ticket_id == ticket_id and edge.id is not None
        }
        current = set(current_ids)
        wanted = {label_to_id[label] for label in wanted_labels if label in label_to_id}
        added = wanted - current
        removed = current - wanted
        if session_id is not None:
            # In-session dependency ops are phase-classified: surface a denial /
            # rollback (e.g. deps are forbidden before cross_validation) instead of
            # swallowing the lifecycle envelope.
            if added:
                self._announce(
                    self.data.action(
                        session_id,
                        "create_dependencies",
                        {"dependencies": [{"fromTicketId": ticket_id, "toTicketId": dep} for dep in sorted(added)]},
                    )
                )
            if removed:
                self._announce(
                    self.data.action(
                        session_id,
                        "delete_dependencies",
                        {"dependencyIds": [current_ids[dep] for dep in sorted(removed)]},
                    )
                )
            return
        for dep in sorted(added):
            self._guard(partial(self.data.add_dependency, ticket_id, dep))
        for dep in sorted(removed):
            self._guard(partial(self.data.remove_dependency, ticket_id, dep))

    # -- central error surface ------------------------------------------------ #

    def _guard(self, fn: Callable[[], object]) -> None:
        try:
            fn()
        except TuiError as err:
            self.push_screen(ErrorPanel(err.err))


def run() -> None:
    """Open the Observatory (the ``specsmither`` console script)."""
    Observatory().run()


if __name__ == "__main__":
    run()
