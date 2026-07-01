"""planner/02 acceptance: stable SVG snapshots (Browser / DAG / Planning / Search).

Determinism does NOT come from the injected report clock (it only feeds
``get_report``, which these screens never call). It comes from: (1) panels render
friendly identifiers (``E1-T2``, titles) — never raw ULIDs; (2) the action log
renders a derived 1-based sequence — never timestamps; (3) ids are minted strictly
monotonic (``new_ulid``) so the merged action/transition order is stable. The
``test_panels_render_no_ulids_or_timestamps`` guard below fails if a raw ULID or
ISO timestamp ever leaks into a rendered panel. Regenerate with
``pytest --snapshot-update``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from textual.widgets import TabbedContent

from specsmither.tui.panels import dag_panel, planning_panel, spec_panel, ticket_panel
from specsmither.tui.screens import SearchView, SpecBrowser
from tests.tui_helpers import drive_to_terminal_review, make_app

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.pilot import Pilot

_SIZE = (120, 40)

#: A 26-char Crockford-base32 ULID, and an ISO-8601 timestamp — neither may render.
_ULID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _render(renderable: RenderableType) -> str:
    console = Console(width=120, record=True)
    console.print(renderable)
    return console.export_text()


def test_panels_render_no_ulids_or_timestamps(tmp_path: Path) -> None:
    """Determinism guard: no raw ULID / ISO timestamp leaks into any rendered panel."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    sid = drive_to_terminal_review(app.data, spec_id)
    tickets = app.data.tickets_for_spec(spec_id)
    tree = app.data.dependency_tree(spec_id)
    session = app.data.active_session(spec_id)
    panels = [
        ticket_panel(tickets[0], tree),
        spec_panel(app.data.specification(spec_id), tree),
        dag_panel(app.data.specification(spec_id), tickets, tree),
        planning_panel(app.data.specification(spec_id), session, app.data.session_log(sid)),
    ]
    for panel in panels:
        text = _render(panel)
        assert not _ULID_RE.search(text), f"ULID leaked into a panel: {text[:200]}"
        assert not _TS_RE.search(text), f"timestamp leaked into a panel: {text[:200]}"


def test_browser_snapshot(snap_compare, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    ticket_id = app.data.tickets_for_spec(spec_id)[0].id

    async def run_before(pilot: Pilot) -> None:
        await pilot.pause()
        pilot.app.query_one(SpecBrowser).select_ticket(ticket_id)
        await pilot.pause()

    assert snap_compare(app, run_before=run_before, terminal_size=_SIZE)


def test_dag_snapshot(snap_compare, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    app, _ = make_app(tmp_path, seed=True)
    assert snap_compare(app, press=["2"], terminal_size=_SIZE)


def test_planning_snapshot(snap_compare, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    drive_to_terminal_review(app.data, spec_id)

    async def run_before(pilot: Pilot) -> None:
        await pilot.pause()
        pilot.app.query_one(TabbedContent).active = "planning"
        await pilot.pause()

    assert snap_compare(app, run_before=run_before, terminal_size=_SIZE)


def test_search_snapshot(snap_compare, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    app, _ = make_app(tmp_path, seed=True)

    async def run_before(pilot: Pilot) -> None:
        await pilot.pause()
        pilot.app.query_one(TabbedContent).active = "search"
        view = pilot.app.query_one(SearchView)
        view._query = "task"
        view._run()
        await pilot.pause()

    assert snap_compare(app, run_before=run_before, terminal_size=_SIZE)
