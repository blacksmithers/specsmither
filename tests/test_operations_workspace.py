"""Workspace + DB-location resolution (``operations/workspace.py``).

The SQLite DB is user-global (one file for every project); a workspace is a
directory bound to one project via ``.specsmither/config.json``. These tests pin
the path-resolution precedence, the lazy ``init`` (create-the-DB-if-absent), the
idempotent re-init, and the headline contract: **two workspaces serve two
different projects off the one DB file**.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import Project
from specsmither.operations.workspace import (
    DB_ENV,
    HOME_ENV,
    PROJECT_ENV,
    WorkspaceConfig,
    init,
    load_workspace_config,
    resolve_context,
    resolve_db_path,
    resolve_home,
    write_workspace_config,
)


def test_resolve_db_path_precedence(tmp_path: Path) -> None:
    # $SPECSMITHER_DB (explicit file) wins outright.
    assert resolve_db_path({DB_ENV: str(tmp_path / "explicit.db")}) == tmp_path / "explicit.db"
    # else $SPECSMITHER_HOME/specsmither.db.
    assert resolve_db_path({HOME_ENV: str(tmp_path / "home")}) == tmp_path / "home" / "specsmither.db"
    # else ~/.specsmither/specsmither.db.
    assert resolve_db_path({}) == Path.home() / ".specsmither" / "specsmither.db"
    assert resolve_home({}) == Path.home() / ".specsmither"


def test_workspace_config_roundtrip(tmp_path: Path) -> None:
    assert load_workspace_config(tmp_path) is None  # absent
    write_workspace_config(tmp_path, WorkspaceConfig(project_id="P1", specification_id="S1"))
    assert (tmp_path / ".specsmither" / "config.json").is_file()
    assert load_workspace_config(tmp_path) == WorkspaceConfig(project_id="P1", specification_id="S1")


def test_init_lazily_creates_db_and_project(tmp_path: Path) -> None:
    env = {HOME_ENV: str(tmp_path / "home")}
    ws = tmp_path / "ws"
    ws.mkdir()

    result = init(ws, project_name="Acme", env=env)

    assert result.created_db is True and result.created_project is True
    assert result.db_path == tmp_path / "home" / "specsmither.db"
    assert result.db_path.exists()
    # The workspace is now bound to the new project.
    config = load_workspace_config(ws)
    assert config is not None and config.project_id == result.project_id
    # …and the project really exists in the user-global DB.
    sf = make_session_factory(init_db(result.db_path))
    with sf() as session:
        project = session.get(Project, result.project_id)
        assert project is not None and project.name == "Acme"


def test_init_is_idempotent(tmp_path: Path) -> None:
    env = {HOME_ENV: str(tmp_path / "home")}
    ws = tmp_path / "ws"
    ws.mkdir()

    first = init(ws, project_name="A", env=env)
    second = init(ws, project_name="A", env=env)  # re-run in the same workspace

    assert second.created_db is False and second.created_project is False
    assert second.project_id == first.project_id


def test_two_workspaces_two_projects_one_db(tmp_path: Path) -> None:
    env = {HOME_ENV: str(tmp_path / "home")}
    ws_a = tmp_path / "alpha"
    ws_b = tmp_path / "beta"
    ws_a.mkdir()
    ws_b.mkdir()

    a = init(ws_a, project_name="ProjA", env=env)
    b = init(ws_b, project_name="ProjB", env=env)

    # Same DB file; the second workspace reuses it (only the first created it).
    assert a.db_path == b.db_path
    assert a.created_db is True and b.created_db is False
    # Two distinct projects, coexisting in the one DB.
    assert a.project_id != b.project_id
    sf = make_session_factory(init_db(a.db_path))
    with sf() as session:
        names = {p.name for p in session.execute(select(Project)).scalars()}
        assert names == {"ProjA", "ProjB"}
    # Each workspace resolves to its own bound project.
    assert resolve_context(ws_a, env).project_id == a.project_id
    assert resolve_context(ws_b, env).project_id == b.project_id


def test_resolve_context_env_project_overrides_workspace(tmp_path: Path) -> None:
    home_env = {HOME_ENV: str(tmp_path / "home")}
    ws = tmp_path / "ws"
    ws.mkdir()
    init(ws, project_name="Bound", env=home_env)

    # $SPECSMITHER_PROJECT beats the workspace file (env > project precedence).
    ctx = resolve_context(ws, {**home_env, PROJECT_ENV: "FORCED_PROJECT"})
    assert ctx.project_id == "FORCED_PROJECT"
    assert ctx.db_path == tmp_path / "home" / "specsmither.db"


def test_init_defaults_project_name_to_workspace_dir(tmp_path: Path) -> None:
    env = {HOME_ENV: str(tmp_path / "home")}
    ws = tmp_path / "my-cool-project"
    ws.mkdir()
    result = init(ws, env=env)  # no explicit name
    sf = make_session_factory(init_db(result.db_path))
    with sf() as session:
        project = session.get(Project, result.project_id)
        assert project is not None and project.name == "my-cool-project"
