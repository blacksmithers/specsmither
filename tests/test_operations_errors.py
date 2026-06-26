"""Unit tests for the CrudError hierarchy + the SQLite → CrudError bridge."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from specsmither.operations.errors import (
    ConflictError,
    CrudError,
    CrudErrorCode,
    NotFoundError,
    PreconditionFailedError,
    SpecSmitherError,
    ValidationFailedError,
    to_crud_error,
)


def _make_integrity_error(message: str) -> IntegrityError:
    """Fabricate a SQLAlchemy IntegrityError whose ``.orig`` str() is *message*.

    No real DB needed — the bridge classifies off the wrapped DBAPI message.
    """

    class FakeOrig(Exception):
        pass

    return IntegrityError("INSERT INTO ...", {}, FakeOrig(message))


def test_subclasses_fix_their_code() -> None:
    assert NotFoundError("Ticket not found: t1").code is CrudErrorCode.NOT_FOUND
    assert ValidationFailedError("bad field").code is CrudErrorCode.VALIDATION_FAILED
    assert ConflictError("already exists").code is CrudErrorCode.CONFLICT
    assert PreconditionFailedError("wrong state").code is CrudErrorCode.PRECONDITION_FAILED


def test_error_class_hierarchy() -> None:
    err = NotFoundError("nope")
    assert isinstance(err, CrudError)
    assert isinstance(err, SpecSmitherError)
    assert isinstance(err, Exception)


def test_crud_error_carries_message_context_and_next_actions() -> None:
    err = ConflictError(
        "Conflict: UNIQUE constraint failed",
        context={"intent": "create"},
        next_actions=["Complete the active session before starting another."],
    )
    assert err.message == "Conflict: UNIQUE constraint failed"
    assert str(err) == "Conflict: UNIQUE constraint failed"
    assert err.context == {"intent": "create"}
    assert err.next_actions == ["Complete the active session before starting another."]


def test_crud_error_defaults_context_and_next_actions_to_none() -> None:
    err = NotFoundError("missing")
    assert err.context is None
    assert err.next_actions is None


def test_code_values_round_trip_as_strings() -> None:
    assert CrudErrorCode.NOT_FOUND == "NOT_FOUND"
    assert CrudErrorCode.VALIDATION_FAILED == "VALIDATION_FAILED"
    assert CrudErrorCode.CONFLICT == "CONFLICT"
    assert CrudErrorCode.PRECONDITION_FAILED == "PRECONDITION_FAILED"


def test_unique_violation_on_create_intent_maps_to_conflict() -> None:
    exc = _make_integrity_error("UNIQUE constraint failed: specifications.id")
    mapped = to_crud_error(exc, intent="create")
    assert isinstance(mapped, ConflictError)
    assert mapped.code is CrudErrorCode.CONFLICT
    assert mapped.__cause__ is exc


def test_unique_violation_on_add_intent_maps_to_conflict() -> None:
    exc = _make_integrity_error("UNIQUE constraint failed: ticket_dependencies.ticket_id")
    mapped = to_crud_error(exc, intent="add")
    assert mapped.code is CrudErrorCode.CONFLICT


def test_unique_violation_on_transition_intent_maps_to_precondition_failed() -> None:
    exc = _make_integrity_error("UNIQUE constraint failed: planning_sessions.specification_id")
    mapped = to_crud_error(exc, intent="transition")
    assert isinstance(mapped, PreconditionFailedError)
    assert mapped.code is CrudErrorCode.PRECONDITION_FAILED
    assert mapped.__cause__ is exc


def test_unique_violation_on_lock_intent_maps_to_precondition_failed() -> None:
    exc = _make_integrity_error("UNIQUE constraint failed: work_sessions.ticket_id")
    mapped = to_crud_error(exc, intent="lock")
    assert mapped.code is CrudErrorCode.PRECONDITION_FAILED


def test_unique_violation_default_intent_maps_to_conflict() -> None:
    exc = _make_integrity_error("UNIQUE constraint failed: specifications.id")
    mapped = to_crud_error(exc)
    assert mapped.code is CrudErrorCode.CONFLICT


def test_foreign_key_violation_maps_to_validation_failed() -> None:
    exc = _make_integrity_error("FOREIGN KEY constraint failed")
    mapped = to_crud_error(exc, intent="create")
    assert isinstance(mapped, ValidationFailedError)
    assert mapped.code is CrudErrorCode.VALIDATION_FAILED


def test_not_null_violation_maps_to_validation_failed() -> None:
    exc = _make_integrity_error("NOT NULL constraint failed: tickets.title")
    mapped = to_crud_error(exc, intent="create")
    assert mapped.code is CrudErrorCode.VALIDATION_FAILED


def test_check_violation_maps_to_validation_failed() -> None:
    exc = _make_integrity_error("CHECK constraint failed: status")
    mapped = to_crud_error(exc, intent="transition")
    assert mapped.code is CrudErrorCode.VALIDATION_FAILED


def test_existing_crud_error_passes_through_unchanged() -> None:
    original = NotFoundError("Ticket not found: t1")
    assert to_crud_error(original, intent="create") is original
    assert to_crud_error(original, intent="transition") is original


def test_unclassified_exception_maps_to_validation_failed() -> None:
    exc = RuntimeError("disk on fire")
    mapped = to_crud_error(exc)
    assert mapped.code is CrudErrorCode.VALIDATION_FAILED
    assert mapped.__cause__ is exc
    assert mapped.context == {"intent": "mutate", "origin": "RuntimeError"}
