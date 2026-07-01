"""Shared Textual widgets: the live ContextBar, the ErrorPanel, and the modals.

The modals are dumb data collectors — they ``dismiss(...)`` a plain dict / value
and let the app decide the mutation path (direct CRUD vs phase-classified APS).
The ``ErrorPanel`` is the single domain-error surface: it renders a normalized
:class:`~specsmither.tui.data.DomainErr` (the ``standard_error`` envelope's
``guidance.prose`` + ``next_actions``), never parsing prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from specsmither.domain.status import TICKET_STATUSES
from specsmither.tui.data import DomainErr
from specsmither.tui.panels import context_line, init_plan_panel

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory

__all__ = [
    "ConfirmModal",
    "ContextBar",
    "ErrorPanel",
    "FeedbackModal",
    "InitModal",
    "SpecEpicEditModal",
    "TicketEditModal",
]


class ContextBar(Static):
    """The always-visible context line (active project · spec · session · ready/blocked)."""

    def reload(self) -> None:
        app = cast("Observatory", self.app)
        try:
            ctx = app.data.context_info(
                project_id=app.active_project_id, specification_id=app.current_spec_id
            )
        except Exception:  # pragma: no cover - a laggy read must never crash the bar
            return
        self.update(context_line(ctx))


class ErrorPanel(ModalScreen[None]):
    """A modal rendering a domain error (``code`` + message + ``next_actions``)."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close"), ("enter", "close", "Close")]

    def __init__(self, err: DomainErr) -> None:
        super().__init__()
        self._err = err

    def compose(self) -> ComposeResult:
        with Vertical(id="error-box"):
            yield Label(Text(f"✖ {self._err.code}", style="bold red"), id="error-title")
            yield Static(Text(self._err.message), id="error-message")
            if self._err.next_actions:
                body = Text()
                body.append("next steps\n", style="dim")
                for action in self._err.next_actions:
                    body.append("• ", style="cyan")
                    body.append(action + "\n")
                yield Static(body, id="error-actions")
            with Horizontal(id="modal-buttons"):
                yield Button("OK", variant="error", id="error-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirm (destructive ops: delete a ticket / spec / project)."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "no", "Cancel")]

    def __init__(self, message: str, *, confirm_label: str = "Yes") -> None:
        super().__init__()
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self._message, id="modal-title")
            with Horizontal(id="modal-buttons"):
                yield Button(self._confirm_label, variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class FeedbackModal(ModalScreen[str | None]):
    """Collect reject-handover feedback (returned to the agent)."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Reject handover — feedback returned to the agent:", id="modal-title")
            yield Input(placeholder="e.g. tighten T3 acceptance criteria", id="feedback-input")
            with Horizontal(id="modal-buttons"):
                yield Button("Reject", variant="warning", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#feedback-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(self.query_one("#feedback-input", Input).value)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SpecEpicEditModal(ModalScreen["dict[str, str] | None"]):
    """Create/edit a project / spec / epic — title + description."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(self, *, heading: str, title: str = "", description: str = "") -> None:
        super().__init__()
        self._heading = heading
        self._title = title
        self._desc = description

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self._heading, id="modal-title")
            yield Label("title")
            yield Input(value=self._title, id="f-title")
            yield Label("description")
            yield TextArea(self._desc, id="f-desc")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-title", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(
                {
                    "title": self.query_one("#f-title", Input).value,
                    "description": self.query_one("#f-desc", TextArea).text,
                }
            )
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TicketEditModal(ModalScreen["dict[str, Any] | None"]):
    """Create/edit a ticket — title, description, (status), AC / steps / files, deps.

    Acceptance criteria are one BDD triple per line, ``given | when | then``; steps
    and files (to create) are one per line; dependencies are comma-separated friendly
    ticket labels (``E1-T2``). The app maps labels → ids and picks the mutation path.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        heading: str,
        title: str = "",
        description: str = "",
        status: str = "pending",
        acceptance: list[str] | None = None,
        steps: list[str] | None = None,
        files: list[str] | None = None,
        depends_on: list[str] | None = None,
        show_status: bool = True,
        show_planning_fields: bool = True,
    ) -> None:
        super().__init__()
        self._heading = heading
        self._title = title
        self._desc = description
        self._status = status
        self._ac = list(acceptance or [])
        self._steps = list(steps or [])
        self._files = list(files or [])
        self._deps = list(depends_on or [])
        self._show_status = show_status
        # AC / steps / files / deps are hidden when the create path cannot persist
        # them (an in-session create_ticket only takes title/description).
        self._show_planning_fields = show_planning_fields

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box-lg"):
            yield Label(self._heading, id="modal-title")
            with VerticalScroll(id="modal-scroll"):
                yield Label("title")
                yield Input(value=self._title, id="f-title")
                yield Label("description")
                yield TextArea(self._desc, id="f-desc")
                if self._show_status:
                    yield Label("status")
                    yield Select(
                        [(s, s) for s in TICKET_STATUSES],
                        value=self._status,
                        allow_blank=False,
                        id="f-status",
                    )
                if self._show_planning_fields:
                    yield Label("acceptance criteria  (one per line: given | when | then)")
                    yield TextArea("\n".join(self._ac), id="f-ac")
                    yield Label("steps  (one per line)")
                    yield TextArea("\n".join(self._steps), id="f-steps")
                    yield Label("files to create  (one per line)")
                    yield TextArea("\n".join(self._files), id="f-files")
                    yield Label("depends on  (comma-separated labels, e.g. E1-T1, E2-T1)")
                    yield Input(value=", ".join(self._deps), id="f-deps")
            with Horizontal(id="modal-buttons"):
                yield Button("Save", variant="success", id="confirm")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#f-title", Input).focus()

    def _lines(self, selector: str) -> list[str]:
        return [
            line.strip()
            for line in self.query_one(selector, TextArea).text.splitlines()
            if line.strip()
        ]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "confirm":
            self.dismiss(None)
            return
        data: dict[str, Any] = {
            "title": self.query_one("#f-title", Input).value,
            "description": self.query_one("#f-desc", TextArea).text,
        }
        if self._show_planning_fields:
            data["acceptance_criteria"] = self._lines("#f-ac")
            data["steps"] = self._lines("#f-steps")
            data["files"] = self._lines("#f-files")
            data["depends_on"] = [
                d.strip() for d in self.query_one("#f-deps", Input).value.split(",") if d.strip()
            ]
        if self._show_status:
            data["status"] = str(self.query_one("#f-status", Select).value)
        self.dismiss(data)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InitModal(ModalScreen[None]):
    """``init`` — bootstrap the workspace (DB + project + config + .claude scaffold)."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("init — scaffold the workspace", id="modal-title")
            # The plan can be long (skills + agents + hooks + settings); keep it in a
            # bounded scroll so the action buttons stay on-screen on small terminals.
            with VerticalScroll(id="init-scroll"):
                yield Static(id="init-status")
                yield Static(id="init-plan")
            with Horizontal(id="modal-buttons"):
                yield Button("Initialize", variant="success", id="init-run")
                yield Button("Close", id="init-close")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self, emitted: list[tuple[str, str, str]] | None = None) -> None:
        from specsmither.tui import scaffold

        app = cast("Observatory", self.app)
        state = "[green]✓ initialized[/]" if app.data.initialized else "[yellow]not initialized[/]"
        self.query_one("#init-status", Static).update(
            Text.from_markup(f"workspace: {state}    MCP wire: [bold]TOON[/]")
        )
        items = emitted if emitted is not None else scaffold.plan_items(app.data.cwd)
        self.query_one("#init-plan", Static).update(init_plan_panel(items))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "init-run":
            self.dismiss(None)
            return
        app = cast("Observatory", self.app)
        already, emitted = app.run_init()
        self._refresh(emitted)
        app.notify(
            "init: already initialized (idempotent)." if already else "init: workspace scaffolded."
        )

    def action_close(self) -> None:
        self.dismiss(None)
