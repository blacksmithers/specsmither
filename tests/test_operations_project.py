"""Project CRUD operations (``operations/project.py``) — the TUI-direct surface.

Projects are the user-global container the TUI manages in-process (NOT an MCP
tool). These tests drive the ops against a real ``tmp_path`` SQLite DB and pin:
round-trip + patch semantics, the count columns staying recompute-owned, the
whole-project FK cascade on delete, the project rollup reflecting its specs, and
that project mutation is deliberately absent from the agent (MCP) tool surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    AcceptanceCriterion,
    Blueprint,
    Epic,
    PlanningSession,
    Project,
    Specification,
    Ticket,
)
from specsmither.dispatch.facade import HANDOVER_TOOL_NAMES, TOOL_NAMES
from specsmither.operations.crud import create_specification
from specsmither.operations.errors import NotFoundError
from specsmither.operations.project import create_project, delete_project, update_project


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / "project.db"))


def _count(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_create_project_round_trips(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    rec = create_project(sf, name="Acme", description="the acme project")

    assert rec.id and rec.user_id == "local"
    assert rec.name == "Acme" and rec.description == "the acme project"
    assert rec.spec_count == 0 and rec.draft_spec_count == 0  # counts start empty
    with sf() as session:
        row = session.get(Project, rec.id)
        assert row is not None and row.name == "Acme" and row.description == "the acme project"


def test_create_project_honours_explicit_id(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    rec = create_project(sf, name="Fixed", project_id="01PROJECTFIXED0000000000A")
    assert rec.id == "01PROJECTFIXED0000000000A"


def test_update_project_patches_name_and_description(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    rec = create_project(sf, name="Old", description="old")
    updated = update_project(sf, rec.id, {"name": "New", "description": "new"})
    assert updated.name == "New" and updated.description == "new"


def test_update_project_ignores_count_columns(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    rec = create_project(sf, name="X")
    # A caller passing a count column must NOT be able to write it (recompute-owned).
    updated = update_project(sf, rec.id, {"spec_count": 99, "name": "Y"})
    assert updated.name == "Y" and updated.spec_count == 0


def test_update_project_missing_raises_not_found(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    with pytest.raises(NotFoundError):
        update_project(sf, "01NOSUCHPROJECT000000000Z", {"name": "X"})


def test_delete_project_missing_raises_not_found(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    with pytest.raises(NotFoundError):
        delete_project(sf, "01NOSUCHPROJECT000000000Z")


def test_delete_project_cascades_whole_subtree(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    project_id = "01PROJECT0000000000000000A"
    spec_id = "01SPEC000000000000000000A"
    epic_id = "01EPIC000000000000000000A"
    ticket_id = "01TICKET00000000000000000A"

    # Seed a full project subtree directly (one level above the M0 cascade test).
    with sf.begin() as session:
        session.add(Project(id=project_id, name="P"))
        session.add(
            Specification(id=spec_id, project_id=project_id, title="S", status="draft")
        )
        session.add(
            Epic(
                id=epic_id,
                specification_id=spec_id,
                epic_number=1,
                title="E",
                description="d",
                objective="o",
            )
        )
        session.add(Ticket(id=ticket_id, epic_id=epic_id, title="T"))
        session.flush()
        session.add(
            AcceptanceCriterion(
                ticket_id=ticket_id, given="g", when="w", then="t", order=1
            )
        )
        session.add(
            Blueprint(
                specification_id=spec_id,
                category="architecture",
                title="BP",
                content="graph",
            )
        )
        session.add(PlanningSession(specification_id=spec_id))

    result = delete_project(sf, project_id)

    assert result.deleted_id == project_id
    with sf() as session:
        assert session.get(Project, project_id) is None
        for model in (
            Specification,
            Epic,
            Ticket,
            AcceptanceCriterion,
            Blueprint,
            PlanningSession,
        ):
            assert _count(session, model) == 0, f"{model.__name__} rows survived the cascade"


def test_project_counts_reflect_specs_after_create(tmp_path: Path) -> None:
    sf = _factory(tmp_path)
    project = create_project(sf, name="P")

    # Each create_specification runs the recompute worklist, which re-derives the
    # project's spec-bucket rollups from its surviving specs.
    create_specification(sf, project_id=project.id, title="Spec one")
    create_specification(sf, project_id=project.id, title="Spec two")

    with sf() as session:
        row = session.get(Project, project.id)
        assert row is not None
        assert row.spec_count == 2
        assert row.draft_spec_count == 2  # both freshly created → draft


def test_project_crud_is_not_an_mcp_tool() -> None:
    # Project management is a human (TUI) action, mirroring create_specification —
    # the agent (MCP) operates only WITHIN existing projects.
    for tool in ("create_project", "update_project", "delete_project"):
        assert tool not in TOOL_NAMES
        assert tool not in HANDOVER_TOOL_NAMES
