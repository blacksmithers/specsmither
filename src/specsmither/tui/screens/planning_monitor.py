"""PlanningMonitor (planner/02 #5): phase / score / gate + interactive handover.

The phase/score/gate panel + the **agent action log** (``planning_session_actions``
interleaved with ``planning_phase_transitions``, newest-first). The buttons mirror
the global keys: Start/Advance (``n``), Approve (``a``), Reject (``j``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Static

from specsmither.tui.panels import planning_panel

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory


class PlanningMonitor(VerticalScroll):
    """The planning-session monitor + handover controls for the active spec."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="pm-body"):
            yield Static(id="pm-content", expand=True)
        with Horizontal(id="pm-actions"):
            yield Button("Start / Advance (n)", id="pm-advance", variant="primary")
            yield Button("Approve (a)", id="pm-approve", variant="success")
            yield Button("Reject (j)", id="pm-reject", variant="warning")

    def on_mount(self) -> None:
        self.reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        app = cast("Observatory", self.app)
        match event.button.id:
            case "pm-advance":
                app.action_advance()
            case "pm-approve":
                app.action_approve()
            case "pm-reject":
                app.action_reject()

    def reload(self) -> None:
        app = cast("Observatory", self.app)
        content = self.query_one("#pm-content", Static)
        spec_id = app.current_spec_id
        if spec_id is None:
            content.update(planning_panel(None, None, []))
            return
        spec = app.data.specification(spec_id)
        session = app.data.active_session(spec_id)
        rows = app.data.session_log(session.id) if session is not None else []
        content.update(planning_panel(spec, session, rows))
