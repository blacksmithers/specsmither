"""End-to-end parity tests for the four agent-facing verbs + the dispatch facade.

Work items #9 (``start`` / ``action`` / ``complete``) and #10 (the
:func:`~specsmither.lifecycle.dispatch.run_verb` / ``create_lifecycle().handle``
dispatch). Each verb is driven through the real
:func:`~specsmither.lifecycle.dispatch.run_verb` convenience over a real on-disk
SQLite database (``tmp_path`` via ``init_db`` + ``make_session_factory``), so the
verb's reads, the WritePlan persist, and the in-transaction recompute all run inside
one ``BEGIN IMMEDIATE`` — exactly the production path. A :class:`_StubValidator`
(injected via ``run_verb(..., validator=...)``) drives the gate deterministically and
records every phase it scored, so the tests can pin both the gate outcome AND the
"never re-validates" short-circuits.

Pins:

* SPS on a ``draft`` spec mints one active ``planning_spec`` session and flips the spec
  ``draft`` → ``planning`` (asserted via a fresh read);
* SPS is idempotent on resume — a second call reuses the one active session, never
  minting a second;
* SPS on an ``awaiting_human_review`` session denies (``sps_not_for_awaiting``);
* an APS ``update_spec`` runs the full pipeline, persists the audit action + the
  validator-output blob, and the persisted ``last_gate_result`` matches the validator;
* APS ``get_planning_status`` short-circuits — the validator is NEVER called;
* a forbidden op (``create_epic`` in ``planning_spec``) denies, leaving the session
  untouched;
* CPS denies (``gate_not_passed``) when the gate fails and parks the session →
  ``awaiting_human_review`` when it passes (recording a same-phase ``ai_agent``
  transition);
* the dispatch facade rejects an unknown verb.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.adapters.lifecycle_runner import run_verb
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
from specsmither.lifecycle.dispatch import LifecycleEvent, VerbName
from specsmither.lifecycle.ports import SpecFull, ValidatorOutput

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
EPIC_ID = "01EPIC000000000000000000A"
TICKET_ID = "01TICKET00000000000000000A"


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


class _StubValidator:
    """A deterministic :class:`~specsmither.lifecycle.ports.Validator`.

    Returns a fixed ``gate_result`` / ``local_score`` and records every phase it is
    asked to score in :attr:`calls` — the "did the verb re-validate?" probe.
    """

    def __init__(self, *, gate_result: str = "pass", local_score: float = 0.9) -> None:
        self.gate_result = gate_result
        self.local_score = local_score
        self.calls: list[PlanningPhase] = []

    def validate(
        self,
        spec_full: SpecFull,
        phase: PlanningPhase,
        config: Mapping[str, Any],
        language: str = "en",
    ) -> ValidatorOutput:
        self.calls.append(phase)
        return ValidatorOutput(
            gate_result=cast("Any", self.gate_result),
            local_score=self.local_score,
            per_epic_score={},
            per_ticket_score={},
            findings=[],
            validated_phase=phase,
        )


def _open(tmp_path: Path) -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / "verbs.db"))


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
    status: str = "active",
    current_phase: str = "planning_spec",
    last_validator_output: dict[str, Any] | None = None,
    last_gate_result: str | None = None,
    last_score: float | None = None,
) -> str:
    sid = new_ulid()
    session.add(
        PlanningSession(
            id=sid,
            specification_id=SPEC_ID,
            status=status,
            current_phase=current_phase,
            actions_count=0,
            started_at=now_iso(),
            last_action_at=now_iso(),
            last_validator_output=last_validator_output,
            last_gate_result=last_gate_result,
            last_score=last_score,
        )
    )
    return sid


def _sessions_for_spec(session: Session) -> list[PlanningSession]:
    stmt = select(PlanningSession).where(PlanningSession.specification_id == SPEC_ID)
    return list(session.execute(stmt).scalars())


def _actions(session: Session, sid: str) -> list[PlanningSessionAction]:
    stmt = select(PlanningSessionAction).where(PlanningSessionAction.planning_session_id == sid)
    return list(session.execute(stmt).scalars())


def _transitions(session: Session, sid: str) -> list[PlanningPhaseTransition]:
    stmt = select(PlanningPhaseTransition).where(
        PlanningPhaseTransition.planning_session_id == sid
    )
    return list(session.execute(stmt).scalars())


# --------------------------------------------------------------------------- #
# SPS — start_planning_session                                                #
# --------------------------------------------------------------------------- #


def test_sps_on_draft_creates_session_and_flips_status(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="draft")

    response = run_verb(
        factory,
        LifecycleEvent(verb="start", payload={"specId": SPEC_ID}),
        validator=_StubValidator(),
    )

    assert response.outcome == "success"
    assert response.variant == GuidanceVariant.PHASE_STATUS_REPORT
    assert response.spec_id == SPEC_ID
    assert response.phase == PlanningPhase.PLANNING_SPEC
    assert response.status == PlanningSessionStatus.ACTIVE

    with factory.begin() as s:
        # The spec was auto-transitioned draft → planning.
        spec = s.get(Specification, SPEC_ID)
        assert spec is not None and spec.status == "planning"

        # Exactly one active session, in planning_spec, with the create action.
        sessions = _sessions_for_spec(s)
        assert len(sessions) == 1
        sess = sessions[0]
        assert sess.id == response.session_id
        assert sess.status == "active"
        assert sess.current_phase == "planning_spec"
        assert sess.last_transition_trigger == "auto_initial"

        ops = {a.operation for a in _actions(s, sess.id)}
        assert "start_planning_session" in ops


def test_sps_resume_is_idempotent(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="draft")

    first = run_verb(
        factory,
        LifecycleEvent(verb="start", payload={"specId": SPEC_ID}),
        validator=_StubValidator(),
    )
    # Second SPS re-enters the SAME active session (resume), never mints a second.
    second = run_verb(
        factory,
        LifecycleEvent(verb="start", payload={"specId": SPEC_ID}),
        validator=_StubValidator(),
    )

    assert first.outcome == "success"
    assert second.outcome == "success"
    assert second.session_id == first.session_id

    with factory.begin() as s:
        sessions = _sessions_for_spec(s)
        assert len(sessions) == 1  # idempotent — still one session
        assert sessions[0].status == "active"


def test_sps_on_awaiting_denies(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="awaiting_human_review", current_phase="cross_validation")

    response = run_verb(
        factory,
        LifecycleEvent(verb="start", payload={"specId": SPEC_ID}),
        validator=_StubValidator(),
    )

    assert response.outcome == "denied"
    assert response.variant == GuidanceVariant.DENIED

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "awaiting_human_review"  # untouched
        denied = [a for a in _actions(s, sid) if a.outcome == "denied"]
        assert any(a.operation == "start_planning_session" for a in denied)
        assert denied[0].payload["deny_reason"] == "sps_not_for_awaiting"


# --------------------------------------------------------------------------- #
# APS — action_planning_session                                               #
# --------------------------------------------------------------------------- #


def test_aps_update_spec_runs_pipeline_and_persists(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="active", current_phase="planning_spec")

    validator = _StubValidator(gate_result="pass", local_score=0.92)
    response = run_verb(
        factory,
        LifecycleEvent(
            verb="action",
            payload={
                "sessionId": sid,
                "operation": "update_spec",
                "payload": {"fields": {"description": "A thorough system description."}},
            },
        ),
        validator=validator,
    )

    # The pipeline re-validated exactly once, at the (native) current phase.
    assert validator.calls == [PlanningPhase.PLANNING_SPEC]
    assert response.outcome == "success"
    assert response.gate_result == "pass"
    assert response.variant == GuidanceVariant.GATE_PASSED

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None
        # The gate result the verb persisted matches the validator's verdict.
        assert sess.last_gate_result == "pass"
        assert sess.last_score == pytest.approx(0.92)
        # The validator-output blob is persisted (the single source of truth).
        assert sess.last_validator_output is not None
        assert sess.last_validator_output["gate_result"] == "pass"
        assert sess.last_validator_output["validated_phase"] == "planning_spec"

        # The audit action was appended with the op + success outcome.
        success = [a for a in _actions(s, sid) if a.outcome == "success"]
        assert any(a.operation == "update_spec" for a in success)


def test_aps_get_planning_status_short_circuits_without_validating(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="active", current_phase="planning_spec")

    validator = _StubValidator()
    response = run_verb(
        factory,
        LifecycleEvent(
            verb="action",
            payload={"sessionId": sid, "operation": "get_planning_status"},
        ),
        validator=validator,
    )

    # The poll/resume verb NEVER re-runs the validator.
    assert validator.calls == []
    assert response.outcome == "success"

    with factory.begin() as s:
        ops = {a.operation for a in _actions(s, sid)}
        assert "get_planning_status" in ops


def test_aps_forbidden_op_denies(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="active", current_phase="planning_spec")

    validator = _StubValidator()
    response = run_verb(
        factory,
        LifecycleEvent(
            verb="action",
            payload={"sessionId": sid, "operation": "create_epic", "payload": {"title": "E2"}},
        ),
        validator=validator,
    )

    # Forbidden in planning_spec → denied before any validation.
    assert validator.calls == []
    assert response.outcome == "denied"
    assert response.variant == GuidanceVariant.DENIED

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "active"  # untouched
        denied = [a for a in _actions(s, sid) if a.outcome == "denied"]
        assert any(a.operation == "create_epic" for a in denied)
        assert denied[0].payload["deny_reason"] == "current_phase_must_finish_first"


# --------------------------------------------------------------------------- #
# CPS — complete_planning_session                                             #
# --------------------------------------------------------------------------- #


def test_cps_denies_when_gate_fails(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="active", current_phase="planning_spec")

    validator = _StubValidator(gate_result="fail")
    response = run_verb(
        factory,
        LifecycleEvent(verb="complete", payload={"sessionId": sid}),
        validator=validator,
    )

    # CPS always re-validates (no TTL) — once.
    assert validator.calls == [PlanningPhase.PLANNING_SPEC]
    assert response.outcome == "denied"
    assert response.variant == GuidanceVariant.DENIED

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None and sess.status == "active"  # NOT parked
        denied = [a for a in _actions(s, sid) if a.outcome == "denied"]
        assert any(a.operation == "complete_planning_session" for a in denied)
        assert denied[0].payload["deny_reason"] == "gate_not_passed"


def test_cps_parks_session_when_gate_passes(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")
        sid = _add_session(s, status="active", current_phase="planning_spec")

    validator = _StubValidator(gate_result="pass")
    response = run_verb(
        factory,
        LifecycleEvent(verb="complete", payload={"sessionId": sid}),
        validator=validator,
    )

    assert response.outcome == "success"
    assert response.variant == GuidanceVariant.HUMAN_HANDOVER
    assert response.status == PlanningSessionStatus.AWAITING_HUMAN_REVIEW

    with factory.begin() as s:
        sess = s.get(PlanningSession, sid)
        assert sess is not None
        # Parked for human review — NOT a phase advance.
        assert sess.status == "awaiting_human_review"
        assert sess.current_phase == "planning_spec"

        # A same-phase ai_agent transition was recorded.
        transitions = _transitions(s, sid)
        assert any(
            t.trigger == "ai_agent"
            and t.from_phase == "planning_spec"
            and t.to_phase == "planning_spec"
            for t in transitions
        )
        success = [a for a in _actions(s, sid) if a.outcome == "success"]
        assert any(a.operation == "complete_planning_session" for a in success)


# --------------------------------------------------------------------------- #
# dispatch facade                                                            #
# --------------------------------------------------------------------------- #


def test_dispatch_rejects_unknown_verb(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    with factory.begin() as s:
        _seed_base(s, spec_status="planning")

    event = LifecycleEvent(verb=cast("VerbName", "frobnicate"), payload={"specId": SPEC_ID})
    with pytest.raises(ValueError, match="Unknown lifecycle verb"):
        run_verb(factory, event)
