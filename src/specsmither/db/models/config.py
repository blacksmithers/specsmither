"""The polymorphic ``config`` table — project overrides + frozen spec snapshots.

Deferred from M0 (no lifecycle existed yet); landed here with the 0.1.0 config seam.
One narrow table backs the whole :class:`specsmither.lifecycle.ports.ConfigStore`
Protocol. Each row is one ``(scope, owner_id, domain)`` config delta:

* ``scope`` — ``'project'`` (a live, editable override) or ``'spec'`` (a frozen
  snapshot captured at spec-creation time).
* ``owner_id`` — the ``project_id`` (scope ``'project'``) or ``spec_id`` (scope
  ``'spec'``). It is a *bare* string, NOT a typed foreign key: the column is
  polymorphic over two parent tables, so the existence guarantee lives in the
  write path, not a DB constraint (one narrow table backs every scope/domain).
* ``domain`` — the config namespace: ``'planning'`` (the crucible
  ``ValidatorConfig``) or ``'planning-lifecycle'`` (the lifecycle guidance knobs).
* ``value`` — the overrides/snapshot blob (a sparse ``DeepPartial`` of the domain
  config), stored as JSON ``TEXT``.
* ``schema_version`` — the domain's config-schema version stamped at write time,
  so a future migration can detect a stale snapshot shape.

``UNIQUE(scope, owner_id, domain)`` makes :meth:`ConfigStore.set_project_overrides`
/ :meth:`ConfigStore.set_spec_snapshot` an idempotent upsert — there is exactly one
row per owner per domain.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from specsmither.db.base import Base, IdMixin, JSONType, TimestampMixin

__all__ = ["PlanningConfig"]


class PlanningConfig(IdMixin, TimestampMixin, Base):
    """One ``(scope, owner_id, domain)`` config delta — the backing row of the
    :class:`~specsmither.lifecycle.ports.ConfigStore` seam (see module docstring)."""

    __tablename__ = "config"
    __table_args__ = (UniqueConstraint("scope", "owner_id", "domain"),)

    # 'project' (live override) | 'spec' (frozen snapshot).
    scope: Mapped[str] = mapped_column()
    # project_id (scope='project') | spec_id (scope='spec'). Polymorphic → no FK.
    owner_id: Mapped[str] = mapped_column()
    # 'planning' (ValidatorConfig) | 'planning-lifecycle' (lifecycle guidance).
    domain: Mapped[str] = mapped_column()
    # The overrides/snapshot blob — a sparse DeepPartial of the domain config.
    value: Mapped[Any] = mapped_column(JSONType, nullable=True)
    # The domain's config-schema version stamped at write time.
    schema_version: Mapped[int] = mapped_column(default=1)
