"""DagView (planner/02 #4): live critical-path / edges / ready-blocked / cycles.

Serves the active spec's materialized ``dependency_tree``; live-refreshes on any
dependency / status mutation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from specsmither.tui.panels import dag_panel

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory


class DagView(VerticalScroll, can_focus=True):
    """The dependency-graph view for the active spec."""

    def compose(self) -> ComposeResult:
        yield Static(id="dag-content", expand=True)

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        content = self.query_one("#dag-content", Static)
        app = cast("Observatory", self.app)
        spec_id = app.current_spec_id
        if spec_id is None:
            content.update(Text("No specification selected.", style="dim"))
            return
        try:
            spec = app.data.specification(spec_id)
            tickets = app.data.tickets_for_spec(spec_id)
            tree = app.data.dependency_tree(spec_id)
            content.update(dag_panel(spec, tickets, tree))
        except Exception as exc:  # pragma: no cover - defensive
            content.update(Text(f"(no DAG: {exc})", style="dim"))
