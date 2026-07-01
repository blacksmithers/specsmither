"""Project CRUD primitives — the human-direct (TUI) operations layer.

Projects are a SpecSmither container (no crucible counterpart). Unlike the entity
verbs, project mutations are NOT exposed as MCP tools: creating / renaming /
deleting a project is a *human* action driven by the TUI in-process (mirroring
``create_specification`` being CLI/TUI-direct, not an agent tool). The agent (MCP)
only operates *within* a project that already exists.

These ops are thin: a project has no child-backed arrays to decompose, and its
denormalized count columns are recompute-owned (#14) — recomputed from the
project's specs whenever a spec under it changes, never written here. So:

* ``create_project`` — insert an empty project (counts default 0).
* ``update_project`` — patch ``name`` / ``description`` / ``status`` only.
* ``delete_project`` — ``session.delete`` relying on FK ``ON DELETE CASCADE`` to
  drop the whole project subtree (specs → epics → tickets → child rows +
  planning / work sessions). No recompute (the project is gone).

Each runs in its own ``Session.begin()`` (the one-transaction-per-mutation
invariant); the stores translate constraint violations via ``to_crud_error``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from specsmither.db.base import new_ulid
from specsmither.db.repositories import ProjectRecord, make_stores
from specsmither.operations.crud import DeleteResult

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["create_project", "delete_project", "update_project"]

#: The only fields ``update_project`` patches — counts/timestamps are not user-owned.
_MUTABLE_FIELDS = ("name", "description", "status", "user_id")


def create_project(
    session_factory: sessionmaker[Session],
    *,
    name: str,
    description: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
) -> ProjectRecord:
    """Create a project (counts start at 0; ULID minted when ``project_id`` is omitted)."""

    with session_factory.begin() as session:
        record = ProjectRecord(
            id=project_id or new_ulid(),
            name=name,
            description=description,
            status=status,
        )
        return make_stores(session).projects.create_project(record)


def update_project(
    session_factory: sessionmaker[Session],
    project_id: str,
    changes: Mapping[str, Any],
) -> ProjectRecord:
    """Patch a project's ``name`` / ``description`` / ``status`` (NotFound if missing).

    Count columns in ``changes`` are ignored — they are recompute-owned (#14).
    """

    with session_factory.begin() as session:
        stores = make_stores(session)
        current = stores.projects.get_project(project_id)  # raises NotFoundError if absent
        patch = {field: changes[field] for field in _MUTABLE_FIELDS if field in changes}
        return stores.projects.update_project(current.model_copy(update=patch))


def delete_project(
    session_factory: sessionmaker[Session],
    project_id: str,
) -> DeleteResult:
    """Delete a project and its WHOLE subtree via FK cascade (NotFound if missing)."""

    with session_factory.begin() as session:
        # FK ON DELETE CASCADE drops every descendant (specs → … → child rows,
        # planning + work sessions); no recompute — the project no longer exists.
        make_stores(session).projects.delete_project(project_id)
        return DeleteResult(deleted_id=project_id, specification_id=None, project_id=None)
