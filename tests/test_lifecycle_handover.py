"""End-to-end parity tests for the three handover verbs (work item #9).

Drives the verbs through :func:`~specsmither.adapters.lifecycle_runner.run_handover`
against a real on-disk SQLite database (``tmp_path`` via ``init_db`` +
``make_session_factory``) seeded with a project → spec (``status='planning'``) → epic →
ticket and one planning session parked in ``awaiting_human_review``. Pins:

* ``approve`` at the terminal phase (``cross_validation``) with a passing gate flips the
  spec to ``ready`` and closes the session (the ONLY path to spec ``ready``);
* ``approve`` with a non-passing gate (``fail`` / unset) → ``HANDOVER_GATE_FAILING`` and
  leaves the spec + session untouched;
* ``approve`` at a non-terminal phase advances the phase and reactivates the session,
  recording a ``human_approve`` transition;
* ``reject_with_feedback`` stashes ``pending_human_feedback`` and reactivates the session;
* ``reject`` (no feedback) reactivates the session and records the trigger;
* any handover on a non-awaiting session → ``HANDOVER_NOT_PENDING``;
* empty feedback → ``INVALID_FEEDBACK``; an unknown session → ``SESSION_NOT_FOUND``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.adapters.lifecycle_runner import run_handover
from specsmither.db.base import make_session_factory, new_ulid, now_iso
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    Epic,
    PlanningPhaseTransition,
    PlanningSession,
    PlanningSessionAction,
    Project,
    Specification,
    Ticket,
)
from specsmither.domain.enums import GuidanceVariant, PlanningPhase, PlanningSessionStatus
from specsmither.lifecycle.verbs.approve import approve_handover
from specsmither.lifecycle.verbs.reject import reject_handover
from specsmither.lifecycle.verbs.reject_with_feedback import reject_handover_with_feedback
from specsmither.lifecycle.verbs.types import (
    ApproveHandoverPayload,
    HandoverError,
    HandoverResult,
    RejectHandoverPayload,
    RejectHandoverWithFeedbackPayload,
)

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
EPIC_ID = "01EPIC000000000000000000A"
TICKET_ID = "01TICKET00000000000000000A"


def _open(tmp_path: Path) -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / "handover.db"))


def _seed_base(session: Session, *, spec_status: str = "planning") -> None:
    session.add(Project(id=PROJECT_ID, name="P"))
    session.add(
        Specification(
            id=SPEC_ID,
            project_id=PROJECT_ID,
            title="S",
            status=spec_status,
            specification_type_id=None,
        )
    )
    session.add(
        Epic(
            id=EPIC_ID,
            specification_id=SPEC_ID,
            epic_number=1,
            title="E",
            description="d",
            objective="o",
            order=1,
        )
    )
    session.add(Ticket(id=TICKET_ID, epic_id=EPIC_ID, title="T", ticket_number=1, order=1))
    session.flush()  # parents persisted before any planning_session FK references them


def _add_session(
    session: Session,
    *,
    status: str,
    current_phase: str,
    last_gate_result: str | None = "pass",
    last_score: float | None = 0.9,
    pending_human_feedback: dict[str, Any] | None = None,
) -> str:
    sid = new_ulid()
    session.add(
        PlanningSession(
            id=sid,
            specification_id=SPEC_ID,
            status=status,
            current_phase=current_phase,
            last_gate_result=last_gate_result,
            last_score=last_score,
            actions_count=0,
            started_at=now_iso(),
            pending_human_feedback=pending_human_feedback,
        )
    )
    return sid


def _actions(session: Session, sid: str) -> list[PlanningSessionAction]:
    stmt = select(PlanningSessionAction).where(PlanningSessionAction.planning_session_id == sid)
    return list(session.execute(stmt).scalars())


def _transitions(session: Session, sid: str) -> list[PlanningPhaseTransition]:
    stmt = select(PlanningPhaseTransition).where(
        PlanningPhaseTransition.planning_session_id == sid
    )
    return list(session.execute(stmt).scalars())


# --------------------------------------------------------------------------- #
# approve — terminal (the only path to spec 'ready')                          #
# --------------------------------------------------------------------------- #


def test_approve_terminal_closes_session_and_readies_spec(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s, status="awaiting_human_review", current_phase="cross_validation"
        )

    outcome = run_handover(
        factory, approve_handover, ApproveHandoverPayload(session_id=sid, user_id="u1")
    )

    assert isinstance(outcome, HandoverResult)
    assert outcome.new_status == PlanningSessionStatus.CLOSED
    assert outcome.new_phase == PlanningPhase.CROSS_VALIDATION
    assert outcome.response.variant == GuidanceVariant.SESSION_CLOSED
    assert outcome.response.status == PlanningSessionStatus.CLOSED

    with factory.begin() as s:
        spec = s.get(Specification, SPEC_ID)
        assert spec is not None
        assert spec.status == "ready"  # the only path to 'ready'

        sess = s.get(PlanningSession, sid)
        assert sess is not None
        assert sess.status == "closed"
        assert sess.closed_at is not None
        assert sess.last_transition_trigger == "human_approve"

        ops = {a.operation for a in _actions(s, sid)}
        assert ops == {"phase_complete", "session_closed"}


# --------------------------------------------------------------------------- #
# approve — gate-on-pass precondition (M11.1)                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gate", ["fail", None])
def test_approve_denied_when_gate_not_passing(tmp_path: Path, gate: str | None) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s,
            status="awaiting_human_review",
            current_phase="cross_validation",
            last_gate_result=gate,
        )

    outcome = run_handover(factory, approve_handover, ApproveHandoverPayload(session_id=sid))

    assert isinstance(outcome, HandoverError)
    assert outcome.code == "HANDOVER_GATE_FAILING"

    with factory.begin() as s:
        spec = s.get(Specification, SPEC_ID)
        assert spec is not None and spec.status == "planning"  # untouched
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "awaiting_human_review"  # untouched
        assert _actions(s, sid) == []


# --------------------------------------------------------------------------- #
# approve — non-terminal advance                                              #
# --------------------------------------------------------------------------- #


def test_approve_non_terminal_advances_phase_and_reactivates(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s, status="awaiting_human_review", current_phase="epic_decomposition"
        )

    outcome = run_handover(factory, approve_handover, ApproveHandoverPayload(session_id=sid))

    assert isinstance(outcome, HandoverResult)
    assert outcome.new_status == PlanningSessionStatus.ACTIVE
    assert outcome.new_phase == PlanningPhase.EPIC_EXPANSION
    assert outcome.response.variant == GuidanceVariant.PHASE_ADVANCE

    with factory.begin() as s:
        spec = s.get(Specification, SPEC_ID)
        assert spec is not None and spec.status == "planning"  # spec NOT readied

        sess = s.get(PlanningSession, sid)
        assert sess is not None
        assert sess.status == "active"
        assert sess.current_phase == "epic_expansion"
        assert sess.last_transition_trigger == "human_approve"

        transitions = _transitions(s, sid)
        assert any(
            t.trigger == "human_approve"
            and t.from_phase == "epic_decomposition"
            and t.to_phase == "epic_expansion"
            for t in transitions
        )
        ops = {a.operation for a in _actions(s, sid)}
        assert ops == {"phase_advance", "human_approve"}


# --------------------------------------------------------------------------- #
# reject with feedback                                                        #
# --------------------------------------------------------------------------- #


def test_reject_with_feedback_stashes_feedback_and_reactivates(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s, status="awaiting_human_review", current_phase="cross_validation"
        )

    feedback = "Tighten the acceptance criteria on epic 1."
    outcome = run_handover(
        factory,
        reject_handover_with_feedback,
        RejectHandoverWithFeedbackPayload(session_id=sid, feedback=feedback, user_id="u1"),
    )

    assert isinstance(outcome, HandoverResult)
    assert outcome.new_status == PlanningSessionStatus.ACTIVE
    assert outcome.new_phase == PlanningPhase.CROSS_VALIDATION  # phase unchanged
    assert feedback in outcome.response.guidance

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None
        assert sess.status == "active"
        assert sess.last_transition_trigger == "human_reject_with_feedback"
        assert sess.pending_human_feedback is not None
        assert sess.pending_human_feedback["content"] == feedback
        assert sess.pending_human_feedback["recorded_by_user_id"] == "u1"

        action = next(a for a in _actions(s, sid) if a.operation == "human_reject_with_feedback")
        assert action.payload["human_instruction"] == feedback


def test_reject_with_feedback_rejects_empty_feedback(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s, status="awaiting_human_review", current_phase="cross_validation"
        )

    outcome = run_handover(
        factory,
        reject_handover_with_feedback,
        RejectHandoverWithFeedbackPayload(session_id=sid, feedback="   "),
    )

    assert isinstance(outcome, HandoverError)
    assert outcome.code == "INVALID_FEEDBACK"
    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "awaiting_human_review"  # untouched


# --------------------------------------------------------------------------- #
# reject — no feedback                                                        #
# --------------------------------------------------------------------------- #


def test_reject_no_feedback_reactivates_and_records_trigger(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(
            s, status="awaiting_human_review", current_phase="ticket_expansion"
        )

    outcome = run_handover(factory, reject_handover, RejectHandoverPayload(session_id=sid))

    assert isinstance(outcome, HandoverResult)
    assert outcome.new_status == PlanningSessionStatus.ACTIVE
    assert outcome.new_phase == PlanningPhase.TICKET_EXPANSION  # phase unchanged
    assert outcome.response.variant == GuidanceVariant.HUMAN_REJECTED_NO_FEEDBACK

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None
        assert sess.status == "active"
        assert sess.last_transition_trigger == "human_reject_no_feedback"
        assert sess.pending_human_feedback is None
        assert {a.operation for a in _actions(s, sid)} == {"human_reject_no_feedback"}


# --------------------------------------------------------------------------- #
# guards — not-pending / not-found                                            #
# --------------------------------------------------------------------------- #


def test_handover_on_active_session_is_not_pending(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)
        sid = _add_session(s, status="active", current_phase="epic_expansion")

    cases = (
        (approve_handover, ApproveHandoverPayload(session_id=sid)),
        (reject_handover, RejectHandoverPayload(session_id=sid)),
        (reject_handover_with_feedback, RejectHandoverWithFeedbackPayload(session_id=sid, feedback="x")),
    )
    for verb, payload in cases:
        outcome = run_handover(factory, verb, payload)  # type: ignore[arg-type]
        assert isinstance(outcome, HandoverError)
        assert outcome.code == "HANDOVER_NOT_PENDING"

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "active"  # never mutated
        assert _actions(s, sid) == []


def test_handover_on_unknown_session_is_not_found(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s)

    outcome = run_handover(
        factory, approve_handover, ApproveHandoverPayload(session_id="01NOSUCHSESSION00000000Z")
    )
    assert isinstance(outcome, HandoverError)
    assert outcome.code == "SESSION_NOT_FOUND"
