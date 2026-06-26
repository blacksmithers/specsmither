"""Tests for the engine/session/transaction contract in ``specsmither.db.base``.

Covers the three load-bearing guarantees:

1. every pooled connection comes up with ``foreign_keys`` ON, WAL journal mode,
   and the configured ``busy_timeout``;
2. :class:`JSONType` round-trips ``dict``/``list`` and preserves ``None``;
3. ``BEGIN IMMEDIATE`` takes the write lock up front — a second writer on the
   same file is locked out (the inv.3 / single-writer proof).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Integer, String, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Mapped, mapped_column

from specsmither.db.base import (
    Base,
    IdMixin,
    JSONType,
    TimestampMixin,
    make_engine,
    make_session_factory,
    new_ulid,
    now_iso,
)


class _JsonRow(Base):
    """Throwaway table exercising :class:`JSONType` (+ the shared mixins)."""

    __tablename__ = "json_probe"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    payload: Mapped[Any | None] = mapped_column(JSONType, nullable=True)


class _Counter(IdMixin, TimestampMixin, Base):
    """Trivial writable table for the lock test (and mixin composition check)."""

    __tablename__ = "counter"

    n: Mapped[int] = mapped_column(Integer, default=0)


def test_pragmas_applied_on_connect(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "pragma.db", busy_timeout_ms=4321)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 4321
    finally:
        engine.dispose()


def test_jsontype_round_trips_dict_list_and_none(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "json.db")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        a = _JsonRow(id="row-dict", payload={"k": "v", "nested": [1, 2, 3], "u": "café"})
        b = _JsonRow(id="row-list", payload=[1, "two", {"three": 3}])
        c = _JsonRow(id="row-none", payload=None)
        with factory.begin() as session:
            session.add_all([a, b, c])

        with factory() as session:
            got = {r.id: r.payload for r in session.execute(select(_JsonRow)).scalars()}
        assert got["row-dict"] == {"k": "v", "nested": [1, 2, 3], "u": "café"}
        assert got["row-list"] == [1, "two", {"three": 3}]
        assert got["row-none"] is None

        # NULL is stored as SQL NULL, not the JSON literal "null".
        with engine.connect() as conn:
            raw = conn.exec_driver_sql(
                "SELECT payload FROM json_probe WHERE id = 'row-none'"
            ).scalar()
        assert raw is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_begin_immediate_acquires_write_lock(tmp_path: Path) -> None:
    db = tmp_path / "lock.db"
    # Create the schema once (and let WAL files materialize) before the race.
    setup = make_engine(db, busy_timeout_ms=200)
    Base.metadata.create_all(setup)
    setup.dispose()

    engine_a = make_engine(db, busy_timeout_ms=200)
    engine_b = make_engine(db, busy_timeout_ms=200)
    conn_a = engine_a.connect()
    try:
        # A opens a transaction and writes — it now holds the write lock.
        conn_a.begin()
        conn_a.execute(text("INSERT INTO counter (id, created_at, updated_at, n) VALUES "
                            "(:id, :ts, :ts, 1)"),
                       {"id": new_ulid(), "ts": now_iso()})

        # B's BEGIN IMMEDIATE must hit the lock immediately, wait busy_timeout,
        # then fail with "database is locked".
        with pytest.raises(OperationalError) as excinfo, engine_b.connect() as conn_b:
            conn_b.begin()
            conn_b.execute(text("INSERT INTO counter (id, created_at, updated_at, n) "
                                "VALUES (:id, :ts, :ts, 2)"),
                           {"id": new_ulid(), "ts": now_iso()})
        assert "database is locked" in str(excinfo.value)
    finally:
        conn_a.close()
        engine_a.dispose()
        engine_b.dispose()
