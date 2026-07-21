"""``ConfigStoreSqlite`` — the SQLite backing of the lifecycle config seam.

Implements :class:`specsmither.lifecycle.ports.ConfigStore` over the single
polymorphic ``config`` table (:class:`~specsmither.db.models.PlanningConfig`). Two
scopes share the table:

* **project overrides** (``scope='project'``, ``owner_id=project_id``) — the live,
  editable config delta a project carries.
* **spec snapshots** (``scope='spec'``, ``owner_id=spec_id``) — the config frozen
  into a spec at creation time.

``set_*`` is an idempotent **upsert** on ``(scope, owner_id, domain)`` — re-setting
the same triple overwrites the ``value`` blob + ``schema_version`` in place rather
than inserting a second row (the table's ``UNIQUE(scope, owner_id, domain)`` makes
that the one authoritative row). ``get_*`` returns the stored blob or ``None`` when
absent; ``list_*`` returns one entry per domain for the owner.

Session-bound like every ``*StoreSqlite`` (subclasses :class:`SessionStore`): it runs
inside the caller's ``Session.begin()`` and never opens or commits a transaction. The
effective config the lifecycle scores against is then assembled by the pure resolvers
in :mod:`specsmither.lifecycle.config` (``crucible.merge_config`` for the ``planning``
domain; ``deep_merge`` for ``planning-lifecycle``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from specsmither.db.base import new_ulid
from specsmither.db.models import PlanningConfig
from specsmither.db.repositories.base import SessionStore
from specsmither.lifecycle.ports import ProjectConfigEntry, SpecConfigEntry

__all__ = ["ConfigStoreSqlite"]

#: The two ``config.scope`` discriminators.
_SCOPE_PROJECT = "project"
_SCOPE_SPEC = "spec"


class ConfigStoreSqlite(SessionStore):
    """SQLite :class:`specsmither.lifecycle.ports.ConfigStore` over the ``config`` table."""

    # -- internal helpers --------------------------------------------------- #

    def _get_row(self, scope: str, owner_id: str, domain: str) -> PlanningConfig | None:
        """Return the single ``(scope, owner_id, domain)`` row, or ``None``."""
        return self.session.execute(
            select(PlanningConfig).where(
                PlanningConfig.scope == scope,
                PlanningConfig.owner_id == owner_id,
                PlanningConfig.domain == domain,
            )
        ).scalar_one_or_none()

    def _upsert(
        self, scope: str, owner_id: str, domain: str, value: Any, schema_version: int
    ) -> None:
        """Insert or overwrite the ``(scope, owner_id, domain)`` config row in place."""
        row = self._get_row(scope, owner_id, domain)
        if row is None:
            self.session.add(
                PlanningConfig(
                    id=new_ulid(),
                    scope=scope,
                    owner_id=owner_id,
                    domain=domain,
                    value=value,
                    schema_version=schema_version,
                )
            )
            return
        row.value = value
        row.schema_version = schema_version

    def _list(self, scope: str, owner_id: str) -> list[PlanningConfig]:
        """Return every config row for *owner_id* under *scope* (one per domain)."""
        return list(
            self.session.execute(
                select(PlanningConfig).where(
                    PlanningConfig.scope == scope,
                    PlanningConfig.owner_id == owner_id,
                )
            ).scalars()
        )

    # -- project overrides (scope='project') -------------------------------- #

    def get_project_overrides(self, project_id: str, domain: str) -> Any | None:
        """Return the project's override blob for *domain*, or ``None`` if unset."""
        row = self._get_row(_SCOPE_PROJECT, project_id, domain)
        return row.value if row is not None else None

    def set_project_overrides(
        self, project_id: str, domain: str, overrides: Any, schema_version: int
    ) -> None:
        """Upsert the project's override blob for *domain* (idempotent in-txn write)."""
        self._upsert(_SCOPE_PROJECT, project_id, domain, overrides, schema_version)

    def list_project_configs(self, project_id: str) -> list[ProjectConfigEntry]:
        """Return one :class:`ProjectConfigEntry` per domain configured for *project_id*."""
        return [
            ProjectConfigEntry(
                domain=row.domain,
                overrides=row.value,
                schema_version=row.schema_version,
            )
            for row in self._list(_SCOPE_PROJECT, project_id)
        ]

    # -- spec snapshots (scope='spec') -------------------------------------- #

    def get_spec_snapshot(self, spec_id: str, domain: str) -> Any | None:
        """Return the spec's frozen snapshot for *domain*, or ``None`` if unset."""
        row = self._get_row(_SCOPE_SPEC, spec_id, domain)
        return row.value if row is not None else None

    def set_spec_snapshot(
        self, spec_id: str, domain: str, snapshot: Any, schema_version: int
    ) -> None:
        """Upsert the spec's frozen snapshot for *domain* (idempotent in-txn write)."""
        self._upsert(_SCOPE_SPEC, spec_id, domain, snapshot, schema_version)

    def list_spec_configs(self, spec_id: str) -> list[SpecConfigEntry]:
        """Return one :class:`SpecConfigEntry` per domain configured for *spec_id*."""
        return [
            SpecConfigEntry(
                domain=row.domain,
                snapshot=row.value,
                schema_version=row.schema_version,
            )
            for row in self._list(_SCOPE_SPEC, spec_id)
        ]


if TYPE_CHECKING:
    # Structural conformance guard: ConfigStoreSqlite must satisfy the ConfigStore
    # Protocol (caught by mypy --strict, never executed).
    from specsmither.lifecycle.ports import ConfigStore

    def _assert_protocol(store: ConfigStoreSqlite) -> ConfigStore:
        return store
