"""Tests for the M7 dispatch facade (work item #12).

Drives :class:`~specsmither.dispatch.facade.Dispatcher` over a real on-disk SQLite
database (``tmp_path`` via ``init_db`` + ``make_session_factory``) seeded with a project
+ specification, so the routing, the three content envelopes, and the
exception -> ``standard_error`` mapping are all exercised through the production path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory, new_ulid, now_iso
from specsmither.db.migrations import init_db
from specsmither.db.models import PlanningSession, Project, Specification
from specsmither.dispatch.facade import (
    HANDOVER_TOOL_NAMES,
    TOOL_NAMES,
    Dispatcher,
    make_dispatcher,
)
from specsmither.operations.crud import create_specification, update_specification
from specsmither.operations.errors import (
    ConflictError,
    CrudError,
    NotFoundError,
    PreconditionFailedError,
)

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
MISSING_ID = "01MISSING000000000000000A"


# --------------------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------------------


def _open(tmp_path: Path) -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / "facade.db"))


def _seed(factory: sessionmaker[Session], *, status: str = "draft") -> None:
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="P"))
        session.add(
            Specification(
                id=SPEC_ID,
                project_id=PROJECT_ID,
                title="S",
                status=status,
                specification_type_id=None,
            )
        )


def _seed_active_session(factory: sessionmaker[Session]) -> str:
    sid = new_ulid()
    with factory.begin() as session:
        session.add(
            PlanningSession(
                id=sid,
                specification_id=SPEC_ID,
                status="active",
                current_phase="planning_spec",
                actions_count=0,
                started_at=now_iso(),
                last_action_at=now_iso(),
            )
        )
    return sid


# --------------------------------------------------------------------------------------
# tool vocabulary
# --------------------------------------------------------------------------------------


def test_tool_surface_is_18_plus_3_handover() -> None:
    assert len(TOOL_NAMES) == 18
    assert len(HANDOVER_TOOL_NAMES) == 3
    assert "feedback" in TOOL_NAMES
    assert "approve_handover" in HANDOVER_TOOL_NAMES
    # No overlap between the base surface and the handover verbs.
    assert not set(TOOL_NAMES) & set(HANDOVER_TOOL_NAMES)


def test_make_dispatcher_returns_dispatcher(tmp_path: Path) -> None:
    assert isinstance(make_dispatcher(_open(tmp_path)), Dispatcher)


# --------------------------------------------------------------------------------------
# lifecycle — planning verbs (success + denial ride the lifecycle envelope)
# --------------------------------------------------------------------------------------


def test_start_planning_session_returns_lifecycle_success_and_flips_status(
    tmp_path: Path,
) -> None:
    factory = _open(tmp_path)
    _seed(factory, status="draft")

    result = make_dispatcher(factory).dispatch("start_planning_session", {"specId": SPEC_ID})

    assert result["kind"] == "lifecycle"
    assert result["agent_response"]["outcome"] == "success"
    assert result["agent_response"]["spec_id"] == SPEC_ID

    with factory.begin() as session:
        spec = session.get(Specification, SPEC_ID)
        assert spec is not None and spec.status == "planning"


def test_action_denial_rides_lifecycle_envelope(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory, status="planning")
    sid = _seed_active_session(factory)

    result = make_dispatcher(factory).dispatch(
        "action_planning_session",
        {"sessionId": sid, "operation": "definitely_not_a_real_operation"},
    )

    # A denial self-discriminates on the SAME envelope (no separate error code).
    assert result["kind"] == "lifecycle"
    assert result["agent_response"]["outcome"] == "denied"


# --------------------------------------------------------------------------------------
# queries / mutations — bare success payloads (no `kind` wrapper)
# --------------------------------------------------------------------------------------


def test_get_specification_returns_bare_payload(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory, status="draft")

    result = make_dispatcher(factory).dispatch("get", {"type": "specification", "id": SPEC_ID})

    assert "kind" not in result
    assert result["id"] == SPEC_ID
    assert result["status"] == "draft"


def test_reopen_specification_routes_to_the_operation(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="P"))
    spec = create_specification(factory, project_id=PROJECT_ID, title="S")
    update_specification(factory, spec.id, {"status": "ready"})

    result = make_dispatcher(factory).dispatch(
        "reopen_specification", {"specificationId": spec.id}
    )

    assert "kind" not in result
    assert result["status"] == "planning"


def test_feedback_is_a_bare_acknowledgement(tmp_path: Path) -> None:
    result = make_dispatcher(_open(tmp_path)).dispatch("feedback", {"message": "looks good"})

    assert "kind" not in result
    assert result["status"] == "acknowledged"
    assert result["received"] == "looks good"


# --------------------------------------------------------------------------------------
# work verbs — frozen-zone stubs (resolve, never throw)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verb",
    ["start_work_session", "action_work_session", "complete_work_session", "reset_work_session"],
)
def test_work_verb_returns_stub_response(tmp_path: Path, verb: str) -> None:
    result = make_dispatcher(_open(tmp_path)).dispatch(verb, {})

    assert result["status"] == "in_development"
    assert result["verb"] == verb
    assert result["planned_for"] == "0.2.0"


# --------------------------------------------------------------------------------------
# error path — domain errors are CONTENT (standard_error), never raised to the protocol
# --------------------------------------------------------------------------------------


def test_unknown_tool_returns_standard_error_with_guidance(tmp_path: Path) -> None:
    result = make_dispatcher(_open(tmp_path)).dispatch("frobnicate", {})

    assert result["kind"] == "standard_error"
    assert result["code"] == "UNKNOWN_TOOL"
    assert isinstance(result["guidance"]["prose"], str) and result["guidance"]["prose"]
    assert result["guidance"]["next_actions"]
    assert "start_planning_session" in result["context"]["availableTools"]


def test_not_found_returns_standard_error(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory)

    result = make_dispatcher(factory).dispatch(
        "get", {"type": "specification", "id": MISSING_ID}
    )

    assert result["kind"] == "standard_error"
    assert result["code"] == "NOT_FOUND"
    assert result["guidance"]["prose"]
    assert result["guidance"]["next_actions"]


def test_reopen_non_ready_is_precondition_failed(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory, status="draft")

    result = make_dispatcher(factory).dispatch(
        "reopen_specification", {"specificationId": SPEC_ID}
    )

    assert result["kind"] == "standard_error"
    assert result["code"] == "PRECONDITION_FAILED"
    assert result["guidance"]["prose"]
    assert result["guidance"]["next_actions"]


def test_approve_handover_missing_session_is_standard_error(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory)

    result = make_dispatcher(factory).dispatch("approve_handover", {"sessionId": MISSING_ID})

    assert result["kind"] == "standard_error"
    assert result["code"] == "NOT_FOUND"
    assert result["guidance"]["prose"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            NotFoundError("missing", context={"entityType": "spec", "entityId": "x"}),
            "NOT_FOUND",
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
            "CONFLICT",
        ),
        (
            PreconditionFailedError(
                "spec is not ready",
                context={
                    "entityType": "spec",
                    "expectedStatus": "ready",
                    "actualStatus": "draft",
                },
            ),
            "PRECONDITION_FAILED",
        ),
    ],
)
def test_error_guidance_shapes_are_english_with_next_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: CrudError,
    code: str,
) -> None:
    """The acceptance criterion: each error code surfaces English prose + next_actions."""
    factory = _open(tmp_path)
    _seed(factory)

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr("specsmither.dispatch.facade.reopen_specification", _raise)

    result = make_dispatcher(factory).dispatch(
        "reopen_specification", {"specificationId": SPEC_ID}
    )

    assert result["kind"] == "standard_error"
    assert result["code"] == code
    prose = result["guidance"]["prose"]
    assert isinstance(prose, str) and prose  # canonical, agent-directed English
    assert result["guidance"]["next_actions"]  # at least one concrete next step
