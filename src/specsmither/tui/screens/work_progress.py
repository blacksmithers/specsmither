"""WorkProgress (planner/02 #6): the 4-dimension assay view — a 0.2.0 preview.

Read-only; the work lifecycle + gate land in planner/03 (over ``assay``). Renders
the shape the screen will take over the active spec's tickets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from specsmither.tui.panels import work_panel

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory


class WorkProgress(VerticalScroll, can_focus=True):
    """The (preview) assay dimensions view for the active spec."""

    def compose(self) -> ComposeResult:
        yield Static(id="work-content", expand=True)

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        content = self.query_one("#work-content", Static)
        app = cast("Observatory", self.app)
        spec_id = app.current_spec_id
        if spec_id is None:
            content.update(Text("No specification selected.", style="dim"))
            return
        try:
            spec = app.data.specification(spec_id)
            tickets = app.data.tickets_for_spec(spec_id)
            content.update(work_panel(spec, tickets))
        except Exception as exc:  # pragma: no cover - defensive
            content.update(Text(f"(no work view: {exc})", style="dim"))
