"""Workspace + DB-location resolution — the TUI's bootstrap seam (no CLI).

The SQLite database is **user-global**: a single ``specsmither.db`` under the user
home holds *every* project. A **workspace** is any directory carrying a
``.specsmither/config.json`` that binds it to one ``projectId`` inside that shared
DB — so different workspaces serve different projects off the same file. This is
exactly what the M0 concurrency design (WAL + ``BEGIN IMMEDIATE`` +
``busy_timeout``) was built for: multiple workspaces / agents on one DB file.

Locations (resolution precedence ``env > project > global > default``):

* DB file: ``$SPECSMITHER_DB`` → ``$SPECSMITHER_HOME/specsmither.db`` →
  ``~/.specsmither/specsmither.db``.
* Global config / home: ``$SPECSMITHER_HOME`` → ``~/.specsmither``.
* Workspace binding: ``<cwd>/.specsmither/config.json`` =
  ``{projectId, specificationId?, planningSessionId?}``.
* Active project override: ``$SPECSMITHER_PROJECT`` beats the workspace file.

``init`` is run **once per workspace**: it lazily creates the user-global DB if it
is absent (else opens + migrates it), then creates (or, when already bound,
reuses) this workspace's project and writes the binding file. Idempotent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.operations.project import create_project

__all__ = [
    "DB_ENV",
    "HOME_ENV",
    "LANGUAGE_ENV",
    "PROJECT_ENV",
    "InitResult",
    "WorkspaceConfig",
    "WorkspaceContext",
    "init",
    "load_workspace_config",
    "resolve_context",
    "resolve_db_path",
    "resolve_home",
    "resolve_language",
    "resolve_workspace_root",
    "write_workspace_config",
]

#: Environment overrides (documented in the README "Runtime / workspace" section).
HOME_ENV = "SPECSMITHER_HOME"
DB_ENV = "SPECSMITHER_DB"
PROJECT_ENV = "SPECSMITHER_PROJECT"
LANGUAGE_ENV = "SPECSMITHER_LANGUAGE"

_DEFAULT_HOME_DIRNAME = ".specsmither"
_DB_FILENAME = "specsmither.db"
_WORKSPACE_DIRNAME = ".specsmither"
_CONFIG_FILENAME = "config.json"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """The user-global SpecSmither home (``$SPECSMITHER_HOME`` → ``~/.specsmither``)."""

    home = _env(env).get(HOME_ENV)
    if home:
        return Path(home).expanduser()
    return Path.home() / _DEFAULT_HOME_DIRNAME


def resolve_db_path(env: Mapping[str, str] | None = None) -> Path:
    """The single user-global DB file (``$SPECSMITHER_DB`` → ``<home>/specsmither.db``)."""

    explicit = _env(env).get(DB_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return resolve_home(env) / _DB_FILENAME


def resolve_language(env: Mapping[str, str] | None = None) -> str:
    """The ambient guidance language (``$SPECSMITHER_LANGUAGE`` → ``"en"``).

    A global default the dispatcher passes to the lifecycle as
    ``LifecyclePorts.default_language``; a per-project / per-spec
    ``planning-lifecycle`` ``guidance.language`` config still wins over it. The raw
    value is passed through verbatim — the lifecycle normalizes it (``pt`` / ``pt_BR``
    → ``pt-br``) and degrades an unsupported tag to ``en``.
    """
    return _str_or_none(_env(env).get(LANGUAGE_ENV)) or "en"


def resolve_workspace_root(cwd: str | Path | None = None) -> Path:
    """The project working-tree root — the grep root for validator file evidence.

    Spec file paths (``src/core/index.py`` …) are relative to the project
    repository root, which is where the ``.specsmither/`` binding lives. Returns the
    nearest ancestor of *cwd* (default: the current directory) holding a
    ``.specsmither/`` directory, or *cwd* itself when none is found. The validator
    adapter probes this root to supply ``existingFiles`` (grep evidence) to crucible.
    """

    start = (Path(cwd) if cwd is not None else Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / _WORKSPACE_DIRNAME).is_dir():
            return candidate
    return start


@dataclass(frozen=True)
class WorkspaceConfig:
    """The per-workspace binding persisted at ``<cwd>/.specsmither/config.json``."""

    project_id: str
    specification_id: str | None = None
    planning_session_id: str | None = None


@dataclass(frozen=True)
class WorkspaceContext:
    """The resolved active context a caller (TUI) operates under."""

    db_path: Path
    project_id: str | None
    specification_id: str | None
    planning_session_id: str | None


def _workspace_config_path(cwd: str | Path) -> Path:
    return Path(cwd) / _WORKSPACE_DIRNAME / _CONFIG_FILENAME


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def load_workspace_config(cwd: str | Path) -> WorkspaceConfig | None:
    """Read ``<cwd>/.specsmither/config.json``; ``None`` if absent or unbound."""

    path = _workspace_config_path(cwd)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    project_id = _str_or_none(data.get("projectId"))
    if project_id is None:
        return None
    return WorkspaceConfig(
        project_id=project_id,
        specification_id=_str_or_none(data.get("specificationId")),
        planning_session_id=_str_or_none(data.get("planningSessionId")),
    )


def write_workspace_config(cwd: str | Path, config: WorkspaceConfig) -> None:
    """Write the workspace binding (creating ``<cwd>/.specsmither/`` as needed)."""

    path = _workspace_config_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {"projectId": config.project_id}
    if config.specification_id:
        payload["specificationId"] = config.specification_id
    if config.planning_session_id:
        payload["planningSessionId"] = config.planning_session_id
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class InitResult:
    """Outcome of :func:`init` — what was created vs. reused."""

    db_path: Path
    project_id: str
    created_db: bool
    created_project: bool


def init(
    cwd: str | Path,
    *,
    project_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> InitResult:
    """Bootstrap a workspace: lazily create the user-global DB, then bind a project.

    Run once per workspace. If the DB file does not exist it is created (schema +
    seeded default specification type); otherwise it is opened and migrated. If the
    workspace is already bound (a valid ``config.json``) the existing project is
    reused (idempotent); otherwise a new project is created (named ``project_name``
    or the workspace directory name) and the binding file is written.
    """

    db_path = resolve_db_path(env)
    created_db = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = init_db(db_path)  # create-or-migrate (idempotent)
    try:
        existing = load_workspace_config(cwd)
        if existing is not None:
            return InitResult(
                db_path=db_path,
                project_id=existing.project_id,
                created_db=created_db,
                created_project=False,
            )
        name = project_name or Path(cwd).resolve().name
        project = create_project(make_session_factory(engine), name=name)
        write_workspace_config(cwd, WorkspaceConfig(project_id=project.id))
        return InitResult(
            db_path=db_path,
            project_id=project.id,
            created_db=created_db,
            created_project=True,
        )
    finally:
        engine.dispose()


def resolve_context(
    cwd: str | Path, env: Mapping[str, str] | None = None
) -> WorkspaceContext:
    """Resolve the active context (DB path + bound project/spec/session).

    Precedence ``env > project > global > default``: the DB path honours
    ``$SPECSMITHER_DB``/``$SPECSMITHER_HOME``; the active project honours
    ``$SPECSMITHER_PROJECT`` over the workspace ``config.json``.
    """

    config = load_workspace_config(cwd)
    env_project = _str_or_none(_env(env).get(PROJECT_ENV))
    return WorkspaceContext(
        db_path=resolve_db_path(env),
        project_id=env_project or (config.project_id if config else None),
        specification_id=config.specification_id if config else None,
        planning_session_id=config.planning_session_id if config else None,
    )
