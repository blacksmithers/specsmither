"""SearchView (planner/02 #7): substring search → jump into the Browser.

Engine-side Python scoring (no FTS5) over the active project's tickets. ``/`` focuses
the query box; tab activation focuses the *results table* (not the Input) so the
``1``–``5`` nav keys are never trapped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.widgets import DataTable, Input, Static

from specsmither.tui.data import TuiError
from specsmither.tui.ui_common import ticket_glyph

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory


class SearchView(Static):
    """Substring search across the active project's tickets."""

    def __init__(self) -> None:
        super().__init__()
        self._query = ""

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="substring search across tickets … (e.g. auth, cursor, gate)",
            id="search-input",
        )
        yield DataTable(id="search-results", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.add_columns("score", "ticket", "title", "status", "matched")
        self.reload()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        self._run()

    def _run(self) -> None:
        table = self.query_one("#search-results", DataTable)
        table.clear()
        app = cast("Observatory", self.app)
        project_id = app.active_project_id
        if not self._query.strip() or project_id is None:
            return
        try:
            result = app.data.search(self._query, project_id=project_id)
        except TuiError:
            return
        for item in result.tickets:
            glyph, _ = ticket_glyph(item.status)
            score = "" if item.relevance_score is None else str(item.relevance_score)
            matched = ", ".join(item.match_reason or [])
            table.add_row(
                score,
                f"T{item.ticket_number}" if item.ticket_number else "—",
                item.title,
                f"{glyph} {item.status}".strip(),
                matched,
                key=item.id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ident = event.row_key.value
        if ident:
            cast("Observatory", self.app).jump_to_ticket(ident)

    def reload(self) -> None:
        self._run()
