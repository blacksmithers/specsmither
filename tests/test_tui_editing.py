"""TUI editing acceptance (planner/02 #9/#10): phase classification + error surface."""

from __future__ import annotations

import asyncio
from pathlib import Path

from specsmither.tui.screens import SpecBrowser
from specsmither.tui.widgets import ContextBar, ErrorPanel, TicketEditModal
from tests.tui_helpers import SEED, drive_to_cross_validation, make_app, tree_labels


def _drive_to_ticket_expansion(data, spec_id: str) -> str:
    start = data.start_session(spec_id)
    sid = start.session_id
    assert sid is not None
    data.action(sid, "update_spec", {"fields": {"description": SEED["spec"]["description"]}})
    data.complete_session(sid)
    data.approve(sid)  # -> epic_decomposition
    data.complete_session(sid)
    data.approve(sid)  # -> epic_expansion
    epic = data.epics(spec_id)[0]
    data.action(sid, "update_epic", {"id": epic.id, "fields": {"description": epic.description}})
    data.complete_session(sid)
    data.approve(sid)  # -> ticket_decomposition
    data.complete_session(sid)
    data.approve(sid)  # -> ticket_expansion
    return sid


def test_forbidden_op_is_denied_with_guidance(tmp_path: Path) -> None:
    """At planning_spec, an update_epic op is forbidden — denied (not a rollback)."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    start = app.data.start_session(spec_id)
    sid = start.session_id
    assert sid is not None
    epic = app.data.epics(spec_id)[0]

    result = app.data.action(sid, "update_epic", {"id": epic.id, "fields": {"description": "x"}})

    assert result.denied is True
    assert result.guidance  # English guidance the TUI toasts
    # the session did not advance off planning_spec
    session = app.data.active_session(spec_id)
    assert session is not None and session.current_phase == "planning_spec"


def test_late_op_rolls_back_to_native_phase(tmp_path: Path) -> None:
    """At ticket_expansion, editing an epic (native epic_expansion) rewinds the session."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    sid = _drive_to_ticket_expansion(app.data, spec_id)
    assert app.data.active_session(spec_id).current_phase == "ticket_expansion"  # type: ignore[union-attr]

    epic = app.data.epics(spec_id)[0]
    result = app.data.action(sid, "update_epic", {"id": epic.id, "fields": {"description": epic.description}})

    assert result.rolled_back is True
    assert result.phase == "epic_expansion"
    session = app.data.active_session(spec_id)
    assert session is not None and session.current_phase == "epic_expansion"


def test_approve_while_active_surfaces_error_panel(tmp_path: Path) -> None:
    """Pressing `a` with the gate not parked → a standard_error → the ErrorPanel modal."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    app.data.start_session(spec_id)  # active @ planning_spec, NOT awaiting review

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("a")  # approve a non-pending handover
            await pilot.pause()
            assert isinstance(app.screen, ErrorPanel)
            assert app.screen._err.code == "PRECONDITION_FAILED"
            assert app.screen._err.message
            assert app.screen._err.next_actions  # at least one concrete next step

    asyncio.run(scenario())


def test_direct_crud_create_spec_and_epic_live_refresh(tmp_path: Path) -> None:
    """Out-of-session (draft) authoring goes DIRECT and the RENDERED tree refreshes."""
    app, _ = make_app(tmp_path, seed=False)
    project = app.data.create_project("Demo")
    app.active_project_id = project.id

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            spec = app.data.create_specification(project.id, "My Spec")
            app.set_current_spec(spec.id)
            app.data.create_epic(spec.id, title="Realtime", description="d", objective="o")
            app.reload_all()
            await pilot.pause()
            # the RENDERED tree (not just the data seam) reflects the new spec + epic
            tree = app.query_one("#browser SpecBrowser", SpecBrowser).query_one("#spec-tree")
            labels = tree_labels(tree.root)
            assert any("My Spec" in label for label in labels)
            assert any("Realtime" in label for label in labels)
            # and the ContextBar re-rendered to the active spec
            assert "My Spec" in str(app.query_one(ContextBar).render())

    asyncio.run(scenario())


def test_in_session_create_ticket_hides_unpersistable_fields(tmp_path: Path) -> None:
    """A mid-session New-ticket modal omits AC/steps/files/deps (it can't persist them)."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    app.data.start_session(spec_id)  # active session -> APS create path
    epic_id = app.data.epics(spec_id)[0].id

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app._create_ticket(epic_id)
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, TicketEditModal)
            assert modal._show_planning_fields is False
            assert len(modal.query("#f-ac")) == 0  # AC field not rendered in-session

    asyncio.run(scenario())


def test_edit_ticket_can_clear_acceptance_criteria(tmp_path: Path) -> None:
    """Emptying the AC field on a DIRECT (draft) edit REPLACES-ALL with nothing."""
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    ticket = app.data.tickets_for_spec(spec_id)[0]
    assert ticket.acceptance_criteria  # seeded with criteria

    # full=True emits the (now empty) array, so update_ticket clears the rows.
    fields = app._ticket_fields_direct({"description": ticket.description or "", "acceptance_criteria": []}, full=True)
    assert fields["acceptance_criteria"] == []
    app.data.update_ticket(ticket.id, fields)
    assert app.data.ticket(ticket.id).acceptance_criteria == []


def test_in_session_remove_draft_authored_dependency(tmp_path: Path) -> None:
    """Removing in-session a dep authored in draft (random ULID id) ACTUALLY deletes it.

    The seed wires deps via the DIRECT path (ULID-id edges). The old delete reconstructed
    the APS-style id `f"{ticket}--requires--{dep}"`, which never matched a ULID edge → the
    removal silently missed. The fix uses the real `DependencyEdge.id`.
    """
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    sid = drive_to_cross_validation(app.data, spec_id)  # deps are native at cross_validation
    edges = app.data.dependencies(spec_id)
    assert edges
    ticket_id = edges[0].ticket_id
    before = {e.depends_on_id for e in edges if e.ticket_id == ticket_id}
    assert before  # the ticket carries >= 1 draft-authored (ULID-id) dependency

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_deps(spec_id, ticket_id, [], session_id=sid)  # remove ALL its deps
            await pilot.pause()

    asyncio.run(scenario())

    after = {e.depends_on_id for e in app.data.dependencies(spec_id) if e.ticket_id == ticket_id}
    assert after == set()  # genuinely removed (the reconstructed-id delete would have missed)
