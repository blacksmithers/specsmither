"""Tests for the dispatch error-guidance layer (work item #13)."""

from __future__ import annotations

import pytest

from specsmither.dispatch.error_guidance import (
    ErrorGuidance,
    ErrorGuidanceCode,
    compose_error_guidance,
    normalise_error,
    unknown_tool_guidance,
)
from specsmither.operations.errors import (
    ConflictError,
    CrudError,
    NotFoundError,
    PreconditionFailedError,
    SpecSmitherError,
    ValidationFailedError,
)


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (
            NotFoundError("missing", context={"entityType": "ticket", "entityId": "t-1"}),
            ErrorGuidanceCode.NOT_FOUND,
        ),
        (
            ValidationFailedError("bad title", context={"failingField": "title"}),
            ErrorGuidanceCode.VALIDATION_FAILED,
        ),
        (
            ConflictError(
                "active session exists",
                context={
                    "entityType": "planning_session",
                    "entityId": "s-1",
                    "conflictingEntityId": "ps-9",
                },
            ),
            ErrorGuidanceCode.CONFLICT,
        ),
        (
            PreconditionFailedError(
                "wrong state",
                context={
                    "entityType": "spec",
                    "entityId": "s-1",
                    "expectedStatus": "planning",
                    "actualStatus": "done",
                },
            ),
            ErrorGuidanceCode.PRECONDITION_FAILED,
        ),
    ],
)
def test_normalise_error_maps_each_crud_subclass(
    exc: CrudError, expected_code: ErrorGuidanceCode
) -> None:
    code, message, guidance, context = normalise_error(exc, tool="get")

    assert code is expected_code
    assert isinstance(guidance, ErrorGuidance)
    # Re-authored English prose (no pt-BR accented characters survive the port).
    assert guidance.prose.strip()
    assert guidance.prose.isascii()
    assert guidance.next_actions
    assert all(isinstance(a, str) and a.strip() for a in guidance.next_actions)
    assert message  # the verb's message is surfaced verbatim
    assert context is exc.context


def test_crud_next_actions_are_appended() -> None:
    exc = NotFoundError(
        "missing",
        context={"entityType": "ticket", "entityId": "t-1"},
        next_actions=["A bespoke verb-authored hint."],
    )
    _code, _message, guidance, _context = normalise_error(exc, tool="get")
    assert "A bespoke verb-authored hint." in guidance.next_actions
    # The composed hints are still present (the bespoke one is appended, not replacing).
    assert len(guidance.next_actions) > 1


def test_unknown_tool_guidance_lists_valid_tools() -> None:
    valid = ["get", "list", "search"]
    code, message, guidance, context = unknown_tool_guidance("frobnicate", valid)

    assert code is ErrorGuidanceCode.UNKNOWN_TOOL
    assert "frobnicate" in message
    assert guidance.prose.isascii()
    for tool in valid:
        assert tool in guidance.prose
    assert guidance.next_actions
    assert context is not None
    assert context["availableTools"] == valid


def test_arbitrary_exception_maps_to_internal() -> None:
    code, message, guidance, context = normalise_error(ValueError("boom"), tool="get")

    assert code is ErrorGuidanceCode.INTERNAL
    assert "boom" in message  # surfaced
    assert isinstance(guidance, ErrorGuidance)
    assert guidance.prose.strip()
    assert guidance.prose.isascii()
    assert guidance.next_actions
    assert context is not None
    assert context["exception"] == "ValueError"


def test_engine_error_maps_to_internal() -> None:
    code, _message, _guidance, context = normalise_error(SpecSmitherError("engine fail"))

    assert code is ErrorGuidanceCode.INTERNAL
    assert context is not None
    assert context["exception"] == "SpecSmitherError"


@pytest.mark.parametrize("code", list(ErrorGuidanceCode))
def test_compose_error_guidance_for_every_code(code: ErrorGuidanceCode) -> None:
    guidance = compose_error_guidance(
        code,
        entity_type="ticket",
        message="something went wrong",
        context={
            "tool": "get",
            "entityId": "x-1",
            "conflictingEntityId": "y-2",
            "failingField": "title",
            "expectedStatus": "planning",
            "actualStatus": "done",
            "requiredScope": "write",
            "availableTools": ["get", "list"],
        },
    )

    assert isinstance(guidance, ErrorGuidance)
    assert guidance.prose.strip()
    assert guidance.prose.isascii()  # English, re-authored
    assert guidance.next_actions
    assert all(a.strip() for a in guidance.next_actions)


def test_compose_conflict_variants_each_name_their_recovery_verb() -> None:
    session = compose_error_guidance(
        ErrorGuidanceCode.CONFLICT,
        entity_type="planning_session",
        context={"tool": "start_planning_session", "conflictingEntityId": "ps-1"},
    )
    assert "complete_planning_session" in session.next_actions[0]

    dependency = compose_error_guidance(
        ErrorGuidanceCode.CONFLICT,
        entity_type="dependency",
        context={"tool": "add_dependency"},
    )
    assert any("dependency" in a for a in dependency.next_actions)
