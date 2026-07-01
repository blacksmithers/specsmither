"""Versioned, forward-only migration runner for the SpecSmither SQLite schema (no Alembic).

A deliberately tiny migrator driven by an explicit version ledger. The schema is a
sequence of numbered steps; :func:`apply_migrations` runs every step the database has
not yet recorded, in order, then stamps each one.

* :data:`BASELINE_VERSION` (1) — the whole M0 schema, materialized by one
  ``Base.metadata.create_all`` plus the single seeded default ``specification_type``.
* :data:`CURRENT_VERSION` (2) — adds the ``config`` table (the deferred-from-M0
  config seam: project overrides + frozen spec snapshots). A fresh database already
  gets ``config`` at the v1 baseline (the :class:`~specsmither.db.models.PlanningConfig`
  model is registered on ``Base.metadata``, so ``create_all`` builds it); the v2 step
  exists so a database stamped at v1 *before* the model existed gets the table created
  idempotently and is then re-stamped to v2. The step is ``checkfirst``-guarded, so
  running it against a fresh database that already has ``config`` is a harmless no-op.

* :data:`schema_migrations` — a standalone version ledger (``version`` PK,
  ``applied_at`` ISO string). Kept on its *own* ``MetaData`` (not ``Base.metadata``)
  so the migration bookkeeping is decoupled from the domain schema and is
  created/queried explicitly, never via ``Base.metadata.create_all``.
* :func:`apply_migrations` — idempotent + re-runnable: ensure the ledger exists, read
  the current version, run each not-yet-applied step (baseline → v2), and record it.
  Re-running at :data:`CURRENT_VERSION` is a no-op.
* :func:`current_version` — the highest applied version (0 if none).
* :func:`init_db` — the one-call bootstrap: build a wired engine, apply migrations,
  return the engine.
* :func:`get_default_specification_type_id` — the seeded default's id (the value
  ``create_specification`` defaults to when no type is supplied).

Importing this module imports :mod:`specsmither.db.models`, which is what registers
every table on ``Base.metadata`` — so ``create_all`` here builds the *whole* schema.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Column, Integer, MetaData, Table, Text, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from specsmither.db.base import make_engine, new_ulid, now_iso
from specsmither.db.models import Base, PlanningConfig, SpecificationType
from specsmither.operations.errors import NotFoundError

__all__ = [
    "BASELINE_VERSION",
    "CURRENT_VERSION",
    "apply_migrations",
    "current_version",
    "get_default_specification_type_id",
    "init_db",
    "schema_migrations",
]

BASELINE_VERSION = 1
#: The highest schema version this runner knows how to reach. Bumped to 2 for the
#: deferred-from-M0 ``config`` table.
CURRENT_VERSION = 2

# The version ledger lives on its own MetaData so it is never entangled with the
# domain schema's create_all/drop_all and can be created/queried on its own.
_migrations_metadata = MetaData()
schema_migrations = Table(
    "schema_migrations",
    _migrations_metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", Text, nullable=False),
)


def current_version(engine: Engine) -> int:
    """Return the highest applied schema version (``0`` if the ledger is empty).

    Ensures the ``schema_migrations`` ledger exists first (idempotent), so this is
    safe to call against a brand-new database file.
    """
    schema_migrations.create(engine, checkfirst=True)
    with engine.connect() as conn:
        latest = conn.execute(select(sa.func.max(schema_migrations.c.version))).scalar()
    return int(latest) if latest is not None else 0


def _record_version(session: Session, version: int) -> None:
    """Stamp *version* into the ledger (caller owns the transaction)."""
    session.execute(
        insert(schema_migrations).values(version=version, applied_at=now_iso())
    )


def _apply_baseline(engine: Engine) -> None:
    """Step v1: build the full ORM schema, seed the default ``specification_type``.

    ``Base.metadata.create_all`` materializes every registered table (including
    ``config``, since :class:`~specsmither.db.models.PlanningConfig` is registered on
    ``Base.metadata``). The default-type seed and the version stamp share one
    ``BEGIN IMMEDIATE`` transaction.
    """
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        existing_default = session.execute(
            select(SpecificationType.id).where(SpecificationType.is_default.is_(True))
        ).scalar_one_or_none()
        if existing_default is None:
            session.add(
                SpecificationType(id=new_ulid(), name="Default", is_default=True)
            )
        _record_version(session, BASELINE_VERSION)


def _apply_v2(engine: Engine) -> None:
    """Step v2: create the ``config`` table idempotently, then stamp v2.

    ``checkfirst=True`` makes the DDL a no-op on a fresh database that already built
    ``config`` at the baseline; on a pre-config v1 database it creates the table.
    """
    # `Metadata.tables[...]` is typed `Table` (has `.create`); `__table__` is `FromClause`.
    PlanningConfig.metadata.tables["config"].create(engine, checkfirst=True)
    with Session(engine) as session, session.begin():
        _record_version(session, CURRENT_VERSION)


def apply_migrations(engine: Engine) -> None:
    """Bring *engine*'s database up to :data:`CURRENT_VERSION`. Idempotent + re-runnable.

    Runs every step the database has not yet recorded, in order, and stamps each one.
    A database already at (or above) :data:`CURRENT_VERSION` returns immediately; a
    fresh database runs the baseline then v2; a database stamped at v1 (before the
    ``config`` table existed) runs only v2.
    """
    cv = current_version(engine)
    if cv >= CURRENT_VERSION:
        return
    if cv < BASELINE_VERSION:
        _apply_baseline(engine)
    if cv < CURRENT_VERSION:
        _apply_v2(engine)


def init_db(path: str | Path, *, busy_timeout_ms: int = 5000) -> Engine:
    """Bootstrap the database at *path* and return its engine (the one-call entry).

    Builds a concurrency-wired engine (``make_engine``), applies migrations, and
    hands back the ready-to-use :class:`~sqlalchemy.engine.Engine`.
    """
    engine = make_engine(path, busy_timeout_ms=busy_timeout_ms)
    apply_migrations(engine)
    return engine


def get_default_specification_type_id(session: Session) -> str:
    """Return the seeded default ``specification_type`` id.

    Raises :class:`~specsmither.operations.errors.NotFoundError` if no default row
    exists (the database was not bootstrapped via :func:`apply_migrations`).
    """
    type_id = session.execute(
        select(SpecificationType.id).where(SpecificationType.is_default.is_(True))
    ).scalar_one_or_none()
    if type_id is None:
        raise NotFoundError(
            "No default specification_type is seeded; run apply_migrations first."
        )
    return type_id
