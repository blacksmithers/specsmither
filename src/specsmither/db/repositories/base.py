"""Shared repository base for the ``*StoreSqlite`` implementations.

Every store is **session-bound**: it holds the caller's :class:`~sqlalchemy.orm.Session`
(the one-Session-per-mutation unit of work) and its mutating methods run *inside*
the caller's ``Session.begin()`` — the WritePlan executor / CRUD primitive owns the
transaction boundary and runs the recompute worklist before commit.

Two hard rules these stores obey (architecture §4, invariants 3+4):

1. **Count columns are NEVER written here.** The TS store interfaces expose ~33
   ``updateX*Count(delta)`` delta-mutators; SpecSmither drops them (no-ops). The
   denormalized counts + cached tree are written **only** by the recompute
   worklist (``rollups/recompute.py``), recomputed from the authoritative child
   rows — so there are no deltas to race.
2. **No auth.** Single local user; the only existence check is null → ``NotFoundError``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from specsmither.operations.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_T = TypeVar("_T")


class SessionStore:
    """Base for the session-bound ``*StoreSqlite`` repositories."""

    def __init__(self, session: Session) -> None:
        self.session = session


def require_found(entity: _T | None, *, kind: str, entity_id: str) -> _T:
    """Return ``entity`` or raise :class:`NotFoundError` (the null → not-found seam)."""
    if entity is None:
        raise NotFoundError(
            f"{kind} {entity_id!r} not found",
            context={"kind": kind, "id": entity_id},
        )
    return entity


__all__ = ["SessionStore", "require_found"]
