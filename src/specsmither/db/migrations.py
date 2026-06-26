"""Versioned baseline migration runner for the SpecSmither SQLite schema (no Alembic).

A deliberately tiny, forward-only migrator. The entire M0 schema is one *baseline*
(``BASELINE_VERSION == 1``): there is no incremental DDL yet, so the runner either
finds a database already at the baseline (and does nothing) or stamps an empty file
from scratch.

* :data:`schema_migrations` — a standalone version ledger (``version`` PK,
  ``applied_at`` ISO string). Kept on its *own* ``MetaData`` (not ``Base.metadata``)
  so the migration bookkeeping is decoupled from the domain schema and is
  created/queried explicitly, never via ``Base.metadata.create_all``.
* :func:`apply_migrations` — idempotent: ensure the ledger exists, read the current
  version, and (if below baseline) run ``Base.metadata.create_all``, seed the single
  default ``specification_type``, and record version 1. Re-running is a no-op.
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
from specsmither.db.models import Base, SpecificationType
from specsmither.operations.errors import NotFoundError

__all__ = [
    "BASELINE_VERSION",
    "apply_migrations",
    "current_version",
    "get_default_specification_type_id",
    "init_db",
    "schema_migrations",
]

BASELINE_VERSION = 1

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


def apply_migrations(engine: Engine) -> None:
    """Bring *engine*'s database up to :data:`BASELINE_VERSION`. Idempotent.

    If the database is already at (or above) the baseline this returns immediately.
    Otherwise it creates the full ORM schema, seeds exactly one default
    ``specification_type`` (only if none exists yet), and records the baseline
    version — all inside a single ``BEGIN IMMEDIATE`` transaction.
    """
    if current_version(engine) >= BASELINE_VERSION:
        return

    Base.metadata.create_all(engine)

    with Session(engine) as session, session.begin():
        existing_default = session.execute(
            select(SpecificationType.id).where(SpecificationType.is_default.is_(True))
        ).scalar_one_or_none()
        if existing_default is None:
            session.add(
                SpecificationType(id=new_ulid(), name="Default", is_default=True)
            )
        session.execute(
            insert(schema_migrations).values(
                version=BASELINE_VERSION, applied_at=now_iso()
            )
        )


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
