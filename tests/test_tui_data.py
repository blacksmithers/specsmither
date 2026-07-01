"""The TUI data seam (planner/02 #2): reads, the two mutation paths, the loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from specsmither.tui.data import Data, TuiError
from tests.tui_helpers import SEED, drive_to_terminal_review, make_app, tui_env


def _seeded_data(tmp_path: Path) -> tuple[Data, str]:
    app, spec_id = make_app(tmp_path, seed=True)
    assert spec_id is not None
    return app.data, spec_id


def test_reads_expose_typed_records(tmp_path: Path) -> None:
    data, spec_id = _seeded_data(tmp_path)
    spec = data.specification(spec_id)
    assert spec.title == SEED["spec"]["title"]
    assert spec.status.value == "draft"
    epics = data.epics(spec_id)
    assert len(epics) == len(SEED["epics"])
    tickets = data.tickets_for_spec(spec_id)
    assert tickets and all(t.epic_id for t in tickets)
    snapshot = data.load_tree()
    assert len(snapshot.projects) == 1
    assert snapshot.projects[0].specs[0].spec.id == spec_id


def test_dependency_tree_and_context(tmp_path: Path) -> None:
    data, spec_id = _seeded_data(tmp_path)
    tree = data.dependency_tree(spec_id)
    assert tree is not None and "summary" in tree
    ctx = data.context_info(project_id=data.context.project_id, specification_id=spec_id)
    assert ctx.spec is not None and ctx.spec.id == spec_id
    assert ctx.ready >= 0 and ctx.blocked >= 0


def test_full_planning_loop_reaches_ready(tmp_path: Path) -> None:
    data, spec_id = _seeded_data(tmp_path)
    sid = drive_to_terminal_review(data, spec_id)
    closed = data.approve(sid)
    assert closed.outcome == "success"
    assert closed.status == "closed"
    assert data.specification(spec_id).status.value == "ready"
    # the action log merged newest-first with a derived seq
    log = data.session_log(sid)
    assert log and log[0].seq == len(log)
    assert {row.kind for row in log} <= {"action", "transition"}


def test_unbound_workspace_has_no_project(tmp_path: Path) -> None:
    env, workspace = tui_env(tmp_path)
    data = Data(cwd=workspace, env=env)
    assert data.initialized is False
    assert data.projects() == []
    data.close()


def test_missing_ticket_raises_tui_error(tmp_path: Path) -> None:
    data, _ = _seeded_data(tmp_path)
    with pytest.raises(TuiError) as excinfo:
        data.ticket("01MISSING000000000000000A")
    assert excinfo.value.err.code == "NOT_FOUND"
