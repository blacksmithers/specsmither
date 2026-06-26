"""Work-session ORM models — the 5 tables of the work lifecycle (release 0.2.0).

The *tables* and the per-ticket lock are built now (M0); the work *verbs* land in
0.2.0. The root :class:`WorkSession` carries a partial-unique index that enforces
the one-active-work-session-per-ticket invariant (the work analogue of the
planning-session lock; architecture §4 / §4a).

The four child tables are the four dimensions of the completion gate (acceptance
checks, implementation-step completions, file changes, test results). Each links
to its work session by a CASCADE foreign key, but links to the *planned* entity it
validates **by value**, not by FK: ``criterion_id`` / ``step_id`` / ``expected_path``
are plain string columns that match the plan's ids/paths — there is deliberately no
foreign key to the planned acceptance-criterion / implementation-step / file-change
row (architecture §3, "link by value not FK").

Column names follow the canonical ``session-types`` schema as corrected in
architecture §13 (``checked``/``checked_at``, ``done``/``done_at``,
``expected_path`` + ``expected_action``/``actual_action``, and the two separate
test-justification arrays).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from specsmither.db.base import Base, IdMixin, JSONType, TimestampMixin, now_iso
from specsmither.domain.enums import JustificationApproved, WorkSessionStatus

__all__ = [
    "WorkSession",
    "WorkSessionAcceptanceCheck",
    "WorkSessionFileChange",
    "WorkSessionImplStepCompletion",
    "WorkSessionTestResult",
]


class WorkSession(IdMixin, TimestampMixin, Base):
    """A work session over one ticket (root of the four gate dimensions).

    The ``ix_one_active_work_session`` partial-unique index is the per-ticket
    guard: at most one non-``closed`` session may exist for a given ticket, so a
    racing second ``start_work_session`` hits a UNIQUE violation → ``CONFLICT``.
    """

    __tablename__ = "work_sessions"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
    )
    status: Mapped[str] = mapped_column(default=WorkSessionStatus.ACTIVE.value)
    started_at: Mapped[str | None] = mapped_column(default=None)
    completed_at: Mapped[str | None] = mapped_column(default=None)
    closed_at: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        Index(
            "ix_one_active_work_session",
            "ticket_id",
            unique=True,
            sqlite_where=text("status != 'closed'"),
        ),
    )


class WorkSessionAcceptanceCheck(IdMixin, Base):
    """Gate dimension 1: one row per ticket acceptance criterion (by value).

    Pre-seeded ``checked=False`` at ``start_work_session`` (one per ticket AC);
    the gate passes this dimension when every row is ``checked``.
    """

    __tablename__ = "work_session_acceptance_checks"

    work_session_id: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"),
    )
    criterion_id: Mapped[str] = mapped_column()
    checked: Mapped[bool] = mapped_column(default=False)
    checked_at: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[str] = mapped_column(default=now_iso)


class WorkSessionImplStepCompletion(IdMixin, Base):
    """Gate dimension 2: one row per ticket implementation step (by value).

    Pre-seeded ``done=False`` at ``start_work_session`` (one per impl step); the
    gate passes this dimension when every row is ``done``.
    """

    __tablename__ = "work_session_impl_step_completions"

    work_session_id: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"),
    )
    step_id: Mapped[str] = mapped_column()
    done: Mapped[bool] = mapped_column(default=False)
    done_at: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[str] = mapped_column(default=now_iso)


class WorkSessionFileChange(IdMixin, Base):
    """Gate dimension 3: an expected-vs-actual file-change reconciliation.

    ``expected_path`` matches the plan's file-change path by value. ``status``
    holds a :class:`~specsmither.domain.enums.FileChangeStatus` value; any
    non-``matched`` row passes the dimension only once ``justification_approved``
    reaches ``approved``.
    """

    __tablename__ = "work_session_file_changes"

    work_session_id: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"),
    )
    expected_path: Mapped[str] = mapped_column()
    # ExpectedAction value (create|modify|delete|reference).
    expected_action: Mapped[str | None] = mapped_column(default=None)
    # ActualAction value (created|modified|deleted|referenced|absent).
    actual_action: Mapped[str | None] = mapped_column(default=None)
    # FileChangeStatus value (matched|missing|mismatched|extra).
    status: Mapped[str | None] = mapped_column(default=None)
    justification: Mapped[str | None] = mapped_column(default=None)
    justification_approved: Mapped[str] = mapped_column(
        default=JustificationApproved.PENDING.value,
    )
    line_count: Mapped[int | None] = mapped_column(default=None)
    commit_hash: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[str] = mapped_column(default=now_iso)


class WorkSessionTestResult(IdMixin, Base):
    """Gate dimension 4: an append-only test-run record for the session.

    The gate passes this dimension when every required row is ``all_passed`` or
    is excused by the per-row ``skipped_justifications`` / ``failure_justifications``.
    """

    __tablename__ = "work_session_test_results"

    work_session_id: Mapped[str] = mapped_column(
        ForeignKey("work_sessions.id", ondelete="CASCADE"),
    )
    test_type: Mapped[str] = mapped_column()
    passed: Mapped[int | None] = mapped_column(default=None)
    failed: Mapped[int | None] = mapped_column(default=None)
    skipped: Mapped[int | None] = mapped_column(default=None)
    total: Mapped[int | None] = mapped_column(default=None)
    all_passed: Mapped[bool | None] = mapped_column(default=None)
    skipped_justifications: Mapped[list[str] | None] = mapped_column(JSONType, default=None)
    failure_justifications: Mapped[list[str] | None] = mapped_column(JSONType, default=None)
    created_at: Mapped[str] = mapped_column(default=now_iso)
