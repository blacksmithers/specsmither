"""SQLite repositories for the five work-session tables (work-lifecycle CRUD).

These are the ``*StoreSqlite`` work-session store implementations — the work-session
store plus the four gate-dimension child stores (acceptance checks,
implementation-step completions, file changes, test results). Only the **CRUD
surface + the per-ticket lock** are M0; the work *verbs*
(gate evaluation, completion, expected-vs-actual reconciliation) land in release
0.2.0. So these stores are deliberately thin: they read and write rows and translate
constraint violations, nothing more.

Contract (architecture §4, invariants 3+4):

* **Session-bound.** Each store subclasses :class:`SessionStore` and runs inside the
  caller's ``Session.begin()``; the mutating methods ``flush`` (to surface a
  constraint at the right call site so it can be translated) but never ``commit`` —
  the WritePlan executor / CRUD primitive owns the transaction boundary.
* **The per-ticket lock.** ``work_sessions`` carries a partial-unique index over
  ``ticket_id`` ``WHERE status != 'closed'``. A racing second live session for the
  same ticket raises :class:`~sqlalchemy.exc.IntegrityError` on flush, translated to
  a :class:`~specsmither.operations.errors.ConflictError` via :func:`to_crud_error`
  (``intent="lock"``).
* **No count writes.** The work tables own no denormalised counts, so there are no
  ``updateX*Count`` delta-mutators to drop here.
* Reads return the ORM rows directly (thin CRUD — no DTO mapping); the existence
  seam is the shared :func:`require_found` (a missing id → ``NotFoundError``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from specsmither.db.base import now_iso
from specsmither.db.models import (
    WorkSession,
    WorkSessionAcceptanceCheck,
    WorkSessionFileChange,
    WorkSessionImplStepCompletion,
    WorkSessionTestResult,
)
from specsmither.db.repositories.base import SessionStore, require_found
from specsmither.domain.enums import JustificationApproved, WorkSessionStatus
from specsmither.operations.errors import to_crud_error

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

__all__ = [
    "WorkSessionAcceptanceCheckStore",
    "WorkSessionFileChangeStore",
    "WorkSessionImplStepCompletionStore",
    "WorkSessionStore",
    "WorkSessionTestResultStore",
]


def _flush(session: Session, *, intent: str = "mutate") -> None:
    """Flush pending DML so a constraint surfaces *here*, translated to a ``CrudError``.

    Flushing (not committing) keeps the caller's transaction open while forcing the
    DB to evaluate UNIQUE / FOREIGN KEY / NOT NULL constraints now, so a violation is
    raised at the originating call site and mapped by :func:`to_crud_error`. ``intent``
    disambiguates a UNIQUE clash: ``"lock"`` → ``PreconditionFailedError`` /
    ``ConflictError`` on the per-ticket lock, the default ``"mutate"`` → ``ConflictError``.
    """
    try:
        session.flush()
    except IntegrityError as exc:
        raise to_crud_error(exc, intent=intent) from exc


class WorkSessionStore(SessionStore):
    """CRUD + per-ticket lock over ``work_sessions`` (root of the four gate dimensions)."""

    def create_work_session(
        self,
        ticket_id: str,
        *,
        status: str = WorkSessionStatus.ACTIVE.value,
        started_at: str | None = None,
    ) -> WorkSession:
        """Open a work session for *ticket_id* (defaults to ``active``).

        The partial-unique index ``ix_one_active_work_session`` enforces the
        one-live-session-per-ticket invariant: a second non-``closed`` session for the
        same ticket hits the UNIQUE constraint on flush and is surfaced as a
        ``ConflictError`` (``intent="lock"``).
        """
        row = WorkSession(
            ticket_id=ticket_id,
            status=status,
            started_at=started_at if started_at is not None else now_iso(),
        )
        self.session.add(row)
        _flush(self.session, intent="lock")
        return row

    def get_work_session(self, work_session_id: str) -> WorkSession:
        """Return the session by id, or raise ``NotFoundError``."""
        row = self.session.get(WorkSession, work_session_id)
        return require_found(row, kind="work_session", entity_id=work_session_id)

    def get_active_for_ticket(self, ticket_id: str) -> WorkSession | None:
        """Return the live (non-``closed``) session for *ticket_id*, or ``None``.

        The partial-unique lock guarantees at most one such row, so this is a safe
        ``one_or_none`` query — it mirrors exactly what the lock protects.
        """
        stmt = select(WorkSession).where(
            WorkSession.ticket_id == ticket_id,
            WorkSession.status != WorkSessionStatus.CLOSED.value,
        )
        return self.session.scalars(stmt).one_or_none()

    def list_work_sessions(
        self,
        *,
        ticket_id: str | None = None,
        status: str | None = None,
    ) -> list[WorkSession]:
        """List sessions, optionally filtered by ticket and/or status (oldest first)."""
        stmt = select(WorkSession)
        if ticket_id is not None:
            stmt = stmt.where(WorkSession.ticket_id == ticket_id)
        if status is not None:
            stmt = stmt.where(WorkSession.status == status)
        stmt = stmt.order_by(WorkSession.created_at)
        return list(self.session.scalars(stmt).all())

    def update_work_session(self, work_session_id: str, **fields: object) -> WorkSession:
        """Patch arbitrary columns on the session (thin CRUD; lifecycle guards land in 0.2.0)."""
        row = self.get_work_session(work_session_id)
        for key, value in fields.items():
            setattr(row, key, value)
        _flush(self.session)
        return row

    def complete_work_session(self, work_session_id: str) -> WorkSession:
        """Mark the session ``completed`` and stamp ``completed_at`` (stays non-closed → still locks)."""
        row = self.get_work_session(work_session_id)
        row.status = WorkSessionStatus.COMPLETED.value
        row.completed_at = now_iso()
        _flush(self.session)
        return row

    def close_work_session(self, work_session_id: str) -> WorkSession:
        """Mark the session ``closed`` and stamp ``closed_at`` (releases the per-ticket lock)."""
        row = self.get_work_session(work_session_id)
        row.status = WorkSessionStatus.CLOSED.value
        row.closed_at = now_iso()
        _flush(self.session)
        return row


class WorkSessionAcceptanceCheckStore(SessionStore):
    """CRUD over ``work_session_acceptance_checks`` (gate dimension 1: ticket ACs by value)."""

    def get_check(self, check_id: str) -> WorkSessionAcceptanceCheck:
        """Return the check by id, or raise ``NotFoundError``."""
        row = self.session.get(WorkSessionAcceptanceCheck, check_id)
        return require_found(row, kind="work_session_acceptance_check", entity_id=check_id)

    def list_by_session(self, work_session_id: str) -> list[WorkSessionAcceptanceCheck]:
        """List the session's acceptance checks (oldest first)."""
        stmt = (
            select(WorkSessionAcceptanceCheck)
            .where(WorkSessionAcceptanceCheck.work_session_id == work_session_id)
            .order_by(WorkSessionAcceptanceCheck.created_at)
        )
        return list(self.session.scalars(stmt).all())

    def upsert_check(
        self,
        work_session_id: str,
        criterion_id: str,
        checked: bool,
    ) -> WorkSessionAcceptanceCheck:
        """Set the ``checked`` flag for one (session, criterion) pair, inserting if absent.

        ``checked_at`` is stamped when *checked* is true and cleared when it is false.
        """
        stmt = select(WorkSessionAcceptanceCheck).where(
            WorkSessionAcceptanceCheck.work_session_id == work_session_id,
            WorkSessionAcceptanceCheck.criterion_id == criterion_id,
        )
        row = self.session.scalars(stmt).one_or_none()
        if row is None:
            row = WorkSessionAcceptanceCheck(
                work_session_id=work_session_id,
                criterion_id=criterion_id,
            )
            self.session.add(row)
        row.checked = checked
        row.checked_at = now_iso() if checked else None
        _flush(self.session)
        return row

    def delete_check(self, check_id: str) -> None:
        """Delete the check by id (raises ``NotFoundError`` if it does not exist)."""
        row = self.get_check(check_id)
        self.session.delete(row)
        _flush(self.session)


class WorkSessionImplStepCompletionStore(SessionStore):
    """CRUD over ``work_session_impl_step_completions`` (gate dimension 2: impl steps by value)."""

    def get_step(self, completion_id: str) -> WorkSessionImplStepCompletion:
        """Return the step completion by id, or raise ``NotFoundError``."""
        row = self.session.get(WorkSessionImplStepCompletion, completion_id)
        return require_found(
            row, kind="work_session_impl_step_completion", entity_id=completion_id
        )

    def list_by_session(self, work_session_id: str) -> list[WorkSessionImplStepCompletion]:
        """List the session's implementation-step completions (oldest first)."""
        stmt = (
            select(WorkSessionImplStepCompletion)
            .where(WorkSessionImplStepCompletion.work_session_id == work_session_id)
            .order_by(WorkSessionImplStepCompletion.created_at)
        )
        return list(self.session.scalars(stmt).all())

    def upsert_step(
        self,
        work_session_id: str,
        step_id: str,
        done: bool,
    ) -> WorkSessionImplStepCompletion:
        """Set the ``done`` flag for one (session, step) pair, inserting if absent.

        ``done_at`` is stamped when *done* is true and cleared when it is false.
        """
        stmt = select(WorkSessionImplStepCompletion).where(
            WorkSessionImplStepCompletion.work_session_id == work_session_id,
            WorkSessionImplStepCompletion.step_id == step_id,
        )
        row = self.session.scalars(stmt).one_or_none()
        if row is None:
            row = WorkSessionImplStepCompletion(
                work_session_id=work_session_id,
                step_id=step_id,
            )
            self.session.add(row)
        row.done = done
        row.done_at = now_iso() if done else None
        _flush(self.session)
        return row

    def delete_step(self, completion_id: str) -> None:
        """Delete the step completion by id (raises ``NotFoundError`` if absent)."""
        row = self.get_step(completion_id)
        self.session.delete(row)
        _flush(self.session)


class WorkSessionFileChangeStore(SessionStore):
    """CRUD over ``work_session_file_changes`` (gate dimension 3: expected-vs-actual files)."""

    def get_file_change(self, file_change_id: str) -> WorkSessionFileChange:
        """Return the file change by id, or raise ``NotFoundError``."""
        row = self.session.get(WorkSessionFileChange, file_change_id)
        return require_found(row, kind="work_session_file_change", entity_id=file_change_id)

    def list_by_session(self, work_session_id: str) -> list[WorkSessionFileChange]:
        """List the session's file-change reconciliations (oldest first)."""
        stmt = (
            select(WorkSessionFileChange)
            .where(WorkSessionFileChange.work_session_id == work_session_id)
            .order_by(WorkSessionFileChange.created_at)
        )
        return list(self.session.scalars(stmt).all())

    def upsert_file_change(
        self,
        work_session_id: str,
        expected_path: str,
        *,
        expected_action: str | None = None,
        actual_action: str | None = None,
        status: str | None = None,
        justification: str | None = None,
        justification_approved: str = JustificationApproved.PENDING.value,
        line_count: int | None = None,
        commit_hash: str | None = None,
    ) -> WorkSessionFileChange:
        """Set the reconciliation for one (session, ``expected_path``) pair, inserting if absent."""
        stmt = select(WorkSessionFileChange).where(
            WorkSessionFileChange.work_session_id == work_session_id,
            WorkSessionFileChange.expected_path == expected_path,
        )
        row = self.session.scalars(stmt).one_or_none()
        if row is None:
            row = WorkSessionFileChange(
                work_session_id=work_session_id,
                expected_path=expected_path,
            )
            self.session.add(row)
        row.expected_action = expected_action
        row.actual_action = actual_action
        row.status = status
        row.justification = justification
        row.justification_approved = justification_approved
        row.line_count = line_count
        row.commit_hash = commit_hash
        _flush(self.session)
        return row

    def delete_file_change(self, file_change_id: str) -> None:
        """Delete the file change by id (raises ``NotFoundError`` if absent)."""
        row = self.get_file_change(file_change_id)
        self.session.delete(row)
        _flush(self.session)


class WorkSessionTestResultStore(SessionStore):
    """Append-only store over ``work_session_test_results`` (gate dimension 4: test runs)."""

    def get_test_result(self, result_id: str) -> WorkSessionTestResult:
        """Return the test result by id, or raise ``NotFoundError``."""
        row = self.session.get(WorkSessionTestResult, result_id)
        return require_found(row, kind="work_session_test_result", entity_id=result_id)

    def list_by_session(self, work_session_id: str) -> list[WorkSessionTestResult]:
        """List the session's test-run records (oldest first)."""
        stmt = (
            select(WorkSessionTestResult)
            .where(WorkSessionTestResult.work_session_id == work_session_id)
            .order_by(WorkSessionTestResult.created_at)
        )
        return list(self.session.scalars(stmt).all())

    def append_test_result(
        self,
        work_session_id: str,
        test_type: str,
        *,
        passed: int | None = None,
        failed: int | None = None,
        skipped: int | None = None,
        total: int | None = None,
        all_passed: bool | None = None,
        skipped_justifications: list[str] | None = None,
        failure_justifications: list[str] | None = None,
    ) -> WorkSessionTestResult:
        """Append a test-run record to the session (append-only — no upsert)."""
        row = WorkSessionTestResult(
            work_session_id=work_session_id,
            test_type=test_type,
            passed=passed,
            failed=failed,
            skipped=skipped,
            total=total,
            all_passed=all_passed,
            skipped_justifications=skipped_justifications,
            failure_justifications=failure_justifications,
        )
        self.session.add(row)
        _flush(self.session)
        return row
