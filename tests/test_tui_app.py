"""TUI acceptance (planner/02): Pilot-driven mount / navigation / handover / init."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Button, TabbedContent

from specsmither.tui.screens import SearchView, SpecBrowser
from specsmither.tui.widgets import InitModal
from tests.tui_helpers import drive_to_terminal_review, make_app, tree_labels


def test_app_mounts_and_navigates(tmp_path: Path) -> None:
    app, spec_id = make_app(tmp_path, seed=True)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()  # let on_mount's call_after_refresh settle focus
            # opens with the seeded spec active + focus in the Browser tree
            assert app.current_spec_id == spec_id
            assert app.active_project_id is not None
            tabs = app.query_one(TabbedContent)
            for key, expected in (("2", "dag"), ("3", "planning"), ("4", "work"), ("5", "search")):
                await pilot.press(key)
                await pilot.pause()
                assert tabs.active == expected
            await pilot.press("1")
            await pilot.pause()
            assert tabs.active == "browser"

    asyncio.run(scenario())


def test_fresh_workspace_is_uninitialized(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, seed=False)

    async def scenario() -> None:
        async with app.run_test():
            assert app.data.initialized is False

    asyncio.run(scenario())


def test_approve_handover_makes_spec_ready(tmp_path: Path) -> None:
    """The headline acceptance: a Pilot `a` on a terminal review flips the spec to ready."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    drive_to_terminal_review(app.data, spec_id)

    # precondition: session parked for review, spec still planning (not ready yet)
    assert app.data.specification(spec_id).status.value == "planning"
    session = app.data.active_session(spec_id)
    assert session is not None and session.status == "awaiting_human_review"

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("a")  # approve the handover
            await pilot.pause()

    asyncio.run(scenario())

    assert app.data.specification(spec_id).status.value == "ready"
    assert app.data.active_session(spec_id) is None  # session closed


def test_init_creates_db_project_config_and_scaffold(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, seed=False)
    workspace = app.data.cwd

    async def scenario() -> None:
        async with app.run_test() as pilot:
            assert app.data.initialized is False
            already, items = app.run_init()
            await pilot.pause()
            assert already is False  # a project was created
            assert app.data.initialized is True
            assert any(status == "created" for _, _, status in items)

    asyncio.run(scenario())

    # workspace binding + DB + scaffold all exist
    assert (workspace / ".specsmither" / "config.json").is_file()
    assert (Path(app.data.context.db_path)).exists()
    assert (workspace / ".claude" / "skills" / "ss-status" / "SKILL.md").is_file()
    assert (workspace / ".claude" / "agents" / "ssag-spec-creator.md").is_file()
    assert (workspace / ".claude" / "settings.local.json").is_file()
    assert app.active_project_id is not None


def test_init_modal_button_scaffolds_on_small_terminal(tmp_path: Path) -> None:
    """The real mechanism: `i` → InitModal → clicking Initialize bootstraps the workspace.

    Uses a 30-row terminal to guard the regression where a tall plan pushed the button
    off-screen; the scrollable plan keeps it clickable.
    """
    app, _ = make_app(tmp_path, seed=False)
    workspace = app.data.cwd

    async def scenario() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.data.initialized is False
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, InitModal)
            app.screen.query_one("#init-run", Button).press()  # the actual button
            await pilot.pause()
            assert app.data.initialized is True

    asyncio.run(scenario())
    assert (workspace / ".specsmither" / "config.json").is_file()
    assert (workspace / ".claude" / "skills" / "ss-status" / "SKILL.md").is_file()


def test_init_is_idempotent(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, seed=False)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            app.run_init()
            await pilot.pause()
            already_second, items = app.run_init()
            await pilot.pause()
            assert already_second is True  # the project was reused
            assert all(status == "exists" for _, _, status in items)

    asyncio.run(scenario())


def test_browser_tree_lists_the_seeded_spec(tmp_path: Path) -> None:
    app, _spec_id = make_app(tmp_path, seed=True)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            browser = app.query_one("#browser SpecBrowser", SpecBrowser)
            tree = browser.query_one("#spec-tree")
            # root → project → spec → epics → tickets all present
            labels = tree_labels(tree.root)
            assert any("Canonical" in label for label in labels)
            assert any(SEED_TITLE in label for label in labels)

    asyncio.run(scenario())


SEED_TITLE = "TaskFlow"


def test_search_jump_opens_browser_on_the_ticket(tmp_path: Path) -> None:
    """planner/02 #7: a search hit jumps into the Browser on that ticket's spec."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    ticket = app.data.tickets_for_spec(spec_id)[0]

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "search"
            view = app.query_one(SearchView)
            view._query = ticket.title.split()[0]
            view._run()
            await pilot.pause()
            app.jump_to_ticket(ticket.id)  # the row-selected handler routes here
            await pilot.pause()
            assert app.query_one(TabbedContent).active == "browser"
            assert app.current_spec_id == spec_id
            assert app.query_one(SpecBrowser).selected_target() == ("ticket", ticket.id)

    asyncio.run(scenario())


def test_selecting_spec_in_another_project_repoints_active_project(tmp_path: Path) -> None:
    """planner/02: navigating to a spec under project B re-points active_project_id to B."""
    app, _ = make_app(tmp_path, seed=False)
    alpha = app.data.create_project("Alpha")
    beta = app.data.create_project("Beta")
    spec_b = app.data.create_specification(beta.id, "Beta Spec")
    app.active_project_id = alpha.id  # start bound to Alpha

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app.set_current_spec(spec_b.id)
            await pilot.pause()
            assert app.active_project_id == beta.id  # follows the spec's own project
            assert app.current_spec_id == spec_b.id

    asyncio.run(scenario())


def test_reject_handover_returns_session_to_agent(tmp_path: Path) -> None:
    """planner/02 #5: rejecting a handover (with feedback) reactivates the session."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    drive_to_terminal_review(app.data, spec_id)
    session = app.data.active_session(spec_id)
    assert session is not None and session.status == "awaiting_human_review"

    # Data.reject (with feedback) routes to reject_handover_with_feedback and reactivates.
    result = app.data.reject(session.id, feedback="tighten the cross-validation coverage")
    assert result.outcome == "success"
    after = app.data.active_session(spec_id)
    assert after is not None and after.status == "active"
    assert app.data.specification(spec_id).status.value == "planning"  # NOT ready
