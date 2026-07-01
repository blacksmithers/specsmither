"""0.1.0 milestone END-TO-END acceptance — the planning lifecycle through the facade.

Drives the real sync engine path (``make_dispatcher`` over a real on-disk SQLite db):
the real crucible gate, the real in-transaction recompute, the real WritePlan executor.
A seed spec is authored CLI-direct via :mod:`specsmither.operations.crud`
(``create_specification`` / ``create_epic`` / ``create_ticket`` / ``create_blueprint`` /
``add_dependency`` / ``link_blueprint_to_ticket`` — NOT MCP tools); every lifecycle step
then rides :meth:`Dispatcher.dispatch` exactly as the L6 MCP server calls it.

The seed (``tests/data/e2e_planning_seed.json``) is a COMPLETE, well-authored spec —
spec-level fields + 2 epics + 8 tickets + 2 blueprints + a valid dependency DAG — derived
from crucible's gate-passing ``seed-cross-validation`` fixture. It is authored so crucible's
authoritative per-phase verdict (``result.passed``) is ``True`` at **all six** planning
phases through the real :class:`CrucibleValidatorAdapter`:

* ``planning_spec`` scores 86.1 (>= 80);
* both epics score >= 80 (>= the 70 ``epic`` threshold) with full requirement / NFR coverage;
* every ticket scores >= 75 (>= the 70 ``ticket`` threshold) — the SpecSmither-unmodelled
  ``codeSnippets`` / ``typeSnippets`` / ``guardrails`` rubric fields are declared ``N/A`` via
  ``fieldDeclarations`` so the rubric still credits them;
* the dependency DAG has no islands, no unjustified roots (the single root carries an
  ``N/A`` ``fieldDeclarations.dependencies`` justification), roots/leaves within the topology
  ratios, every ``filesToBeReferenced`` creator a transitive predecessor, and each blueprint
  linked to >= 2 tickets — so ``cross_validation`` clears too.

Because the adapter now derives ``gate_result`` from crucible's top-level ``result.passed``
(the per-phase verdict composed across the structural / scoring / cross-validation layers)
rather than the *scoring* layer alone, the loop drives ``draft -> ready`` through the REAL
gate at every phase with **no** direct-DB bridge: the headline test only ever talks to
``dispatcher.dispatch``. (Previously the decomposition phases — scoring skipped — and
``cross_validation`` — scoring ``None`` — always folded to ``'fail'``, so the loop could
never reach ``ready``; that adapter bug is fixed and this test exercises the fix.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import crucible
import pytest
from crucible.types import ScoringResultActive, ValidationResult
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.adapters import lifecycle_ports as lifecycle_ports_module
from specsmither.adapters.config import resolve_validator_config
from specsmither.adapters.crucible_validator import CrucibleValidatorAdapter
from specsmither.adapters.lifecycle_ports import SqliteSpecStore
from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import (
    Epic,
    PlanningEntityScoreDatapoint,
    PlanningPhaseTransition,
    PlanningSession,
    PlanningSessionAction,
    Project,
    Specification,
    Ticket,
)
from specsmither.db.repositories.config_store import ConfigStoreSqlite
from specsmither.dispatch.facade import make_dispatcher
from specsmither.domain.enums import PlanningPhase
from specsmither.operations.crud import (
    add_dependency,
    create_blueprint,
    create_epic,
    create_specification,
    create_ticket,
    link_blueprint_to_ticket,
)

# --------------------------------------------------------------------------------------
# seed — a complete spec that clears the real gate at ALL SIX planning phases
# --------------------------------------------------------------------------------------

SEED: dict[str, Any] = json.loads(
    (Path(__file__).parent / "data" / "e2e_planning_seed.json").read_text(encoding="utf-8")
)

PROJECT_ID = "01PROJECT0000000000000000A"
MISSING_ID = "01MISSING000000000000000A"


# --------------------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------------------


def _open(tmp_path: Path) -> sessionmaker[Session]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_session_factory(init_db(tmp_path / "e2e.db"))


def _seed(factory: sessionmaker[Session]) -> str:
    """Author the complete seed spec CLI-direct (NOT via an MCP tool).

    Spec + 2 epics + 8 tickets + 2 blueprints + the ticket→ticket dependency DAG +
    the blueprint↔ticket links, every part carrying the rich content crucible scores.
    """
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="Canonical"))

    spec = create_specification(
        factory,
        project_id=PROJECT_ID,
        title=SEED["spec"]["title"],
        fields={k: v for k, v in SEED["spec"].items() if k != "title"},
    )

    ref_to_id: dict[str, str] = {}
    for epic in SEED["epics"]:
        epic_fields = {
            k: v
            for k, v in epic.items()
            if k not in {"title", "description", "objective", "tickets"}
        }
        created_epic = create_epic(
            factory,
            specification_id=spec.id,
            title=epic["title"],
            description=epic["description"],
            objective=epic["objective"],
            fields=epic_fields,
        )
        for ticket in epic["tickets"]:
            ticket_fields = {k: v for k, v in ticket.items() if k not in {"ref", "title"}}
            created_ticket = create_ticket(
                factory, epic_id=created_epic.id, title=ticket["title"], fields=ticket_fields
            )
            ref_to_id[ticket["ref"]] = created_ticket.id

    blueprint_ids: dict[str, str] = {}
    for blueprint in SEED["blueprints"]:
        blueprint_fields = {
            k: v for k, v in blueprint.items() if k not in {"ref", "title", "category"}
        }
        created_blueprint = create_blueprint(
            factory,
            specification_id=spec.id,
            title=blueprint["title"],
            category=blueprint["category"],
            fields=blueprint_fields,
        )
        blueprint_ids[blueprint["ref"]] = created_blueprint.id

    for link in SEED["blueprintLinks"]:
        link_blueprint_to_ticket(
            factory,
            ref_to_id[link["ticketRef"]],
            blueprint_ids[link["blueprintId"]],
            context=link.get("context"),
            section=link.get("section"),
        )

    for edge in SEED["dependencies"]:
        for depends_on in edge["dependsOn"]:
            add_dependency(factory, ref_to_id[edge["ticket"]], ref_to_id[depends_on])

    return spec.id


def _seed_thin(factory: sessionmaker[Session], *, epics: int) -> str:
    """A deliberately under-decomposed spec: ``epics`` epics, zero tickets.

    Used to prove the corrected adapter FAILS a structurally-incomplete decomposition
    phase (crucible's ``specification.epics`` min-count is 2 at ``epic_decomposition``).
    """
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="Canonical"))
    spec = create_specification(
        factory, project_id=PROJECT_ID, title="Thin", fields={"description": "thin"}
    )
    for index in range(epics):
        create_epic(
            factory,
            specification_id=spec.id,
            title=f"Epic {index}",
            description="d",
            objective="o",
        )
    return spec.id


def _session_id(factory: sessionmaker[Session], spec_id: str) -> str:
    with factory.begin() as session:
        row = (
            session.execute(
                select(PlanningSession).where(PlanningSession.specification_id == spec_id)
            )
            .scalars()
            .one()
        )
        return row.id


def _set_session(factory: sessionmaker[Session], sid: str, **fields: Any) -> None:
    """Directly stamp planning-session columns (the documented DB-injection seam)."""
    with factory.begin() as session:
        row = session.get(PlanningSession, sid)
        assert row is not None
        for key, value in fields.items():
            setattr(row, key, value)


def _spec_status(factory: sessionmaker[Session], spec_id: str) -> str:
    with factory.begin() as session:
        spec = session.get(Specification, spec_id)
        assert spec is not None
        return spec.status


def _count(factory: sessionmaker[Session], model: Any, sid: str) -> int:
    with factory.begin() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(model)
                .where(model.planning_session_id == sid)
            ).scalar_one()
        )


def _first_epic(factory: sessionmaker[Session], spec_id: str) -> tuple[str, str]:
    """The (id, description) of the spec's first epic — for an idempotent ``update_epic``."""
    with factory.begin() as session:
        epic = (
            session.execute(
                select(Epic).where(Epic.specification_id == spec_id).order_by(Epic.order, Epic.id)
            )
            .scalars()
            .first()
        )
        assert epic is not None
        return epic.id, epic.description


def _first_ticket(factory: sessionmaker[Session], spec_id: str) -> tuple[str, str | None]:
    """The (id, description) of the spec's first ticket — for an idempotent ``update_ticket``."""
    with factory.begin() as session:
        epic_ids = (
            session.execute(select(Epic.id).where(Epic.specification_id == spec_id))
            .scalars()
            .all()
        )
        ticket = (
            session.execute(
                select(Ticket)
                .where(Ticket.epic_id.in_(epic_ids))
                .order_by(Ticket.order, Ticket.id)
            )
            .scalars()
            .first()
        )
        assert ticket is not None
        return ticket.id, ticket.description


def _direct_gate(factory: sessionmaker[Session], spec_id: str, phase: PlanningPhase) -> str:
    """A DIRECT ``crucible.validate`` over the projected (persisted) spec → ``result.passed``.

    Mirrors exactly what :class:`CrucibleValidatorAdapter` reads now: the authoritative
    top-level per-phase verdict, NOT the scoring layer alone.
    """
    with factory.begin() as session:
        spec_full = SqliteSpecStore(session).get_spec_full(spec_id)
        assert spec_full is not None
        config = resolve_validator_config(ConfigStoreSqlite(session), PROJECT_ID, spec_id)
        spec_dict = spec_full.spec.model_dump(by_alias=True, exclude_none=True)
        active = [epic.id for epic in spec_full.epics] if phase == PlanningPhase.EPIC_EXPANSION else (
            [t.id for epic in spec_full.epics for t in epic.tickets]
            if phase == PlanningPhase.TICKET_EXPANSION
            else None
        )
    result = crucible.validate(
        spec_dict,
        {
            "phase": phase.value,
            "config": config,
            "activeEntityId": active,
            "returns": ["structural", "scoring", "guidance"],
        },
    )
    return "pass" if result.passed else "fail"


# --------------------------------------------------------------------------------------
# headline — the loop drives draft -> ready through the REAL gate at all six phases
# --------------------------------------------------------------------------------------


def test_planning_loop_reaches_ready_through_the_real_gate(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    spec_id = _seed(factory)
    dispatcher = make_dispatcher(factory)

    def action(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = dispatcher.dispatch(
            "action_planning_session",
            {"sessionId": sid, "operation": operation, "payload": payload},
        )
        assert result["kind"] == "lifecycle"
        return cast("dict[str, Any]", result["agent_response"])

    def complete() -> dict[str, Any]:
        result = dispatcher.dispatch("complete_planning_session", {"sessionId": sid})
        return cast("dict[str, Any]", result["agent_response"])

    def approve() -> dict[str, Any]:
        result = dispatcher.dispatch("approve_handover", {"sessionId": sid, "userId": "dev"})
        assert result["kind"] == "lifecycle"
        return cast("dict[str, Any]", result["agent_response"])

    # --- start_planning_session: draft -> planning, one active planning_spec session ---
    started = dispatcher.dispatch("start_planning_session", {"specId": spec_id})
    assert started["kind"] == "lifecycle"
    assert started["agent_response"]["outcome"] == "success"
    assert started["agent_response"]["phase"] == "planning_spec"
    assert _spec_status(factory, spec_id) == "planning"  # fresh DB read — the flip happened
    sid = _session_id(factory, spec_id)

    # --- planning_spec: the real gate passes; it equals a DIRECT crucible result.passed ---
    spec_action = action("update_spec", {"fields": {"description": SEED["spec"]["description"]}})
    assert spec_action["outcome"] == "success"
    assert spec_action["gate_result"] == "pass"
    assert spec_action["gate_result"] == _direct_gate(factory, spec_id, PlanningPhase.PLANNING_SPEC)
    spec_complete = complete()
    assert spec_complete["outcome"] == "success"
    assert spec_complete["status"] == "awaiting_human_review"
    assert approve()["phase"] == "epic_decomposition"
    assert _spec_status(factory, spec_id) == "planning"  # NOT ready — only at the terminal

    # --- epic_decomposition: a binary phase crucible scores via STRUCTURAL counts. The
    #     corrected adapter passes it (it tracks result.passed, not the skipped scoring) ---
    assert complete()["status"] == "awaiting_human_review"
    assert approve()["phase"] == "epic_expansion"

    # --- epic_expansion: the per-entity gate clears (both epics >= the epic threshold) -----
    epic_id, epic_description = _first_epic(factory, spec_id)
    assert action("update_epic", {"id": epic_id, "fields": {"description": epic_description}})[
        "gate_result"
    ] == "pass"
    assert complete()["status"] == "awaiting_human_review"
    assert approve()["phase"] == "ticket_decomposition"

    # --- ticket_decomposition: another binary/structural phase — the real gate passes ------
    assert complete()["status"] == "awaiting_human_review"
    assert approve()["phase"] == "ticket_expansion"

    # --- ticket_expansion: the per-ticket gate clears (every ticket >= the ticket threshold) -
    ticket_id, ticket_description = _first_ticket(factory, spec_id)
    assert action("update_ticket", {"id": ticket_id, "fields": {"description": ticket_description}})[
        "gate_result"
    ] == "pass"
    assert complete()["status"] == "awaiting_human_review"
    assert approve()["phase"] == "cross_validation"

    # --- cross_validation: the cross-validation layer clears (valid DAG + coverage); the
    #     terminal approve flips the spec to `ready` and closes the session -----------------
    cross_complete = complete()
    assert cross_complete["outcome"] == "success"
    assert cross_complete["status"] == "awaiting_human_review"
    closed = approve()
    assert closed["outcome"] == "success"
    assert closed["status"] == "closed"

    # the spec reached `ready` and the session closed (fresh DB reads) — NO direct-DB bridge.
    assert _spec_status(factory, spec_id) == "ready"
    with factory.begin() as session:
        closed_session = session.get(PlanningSession, sid)
        assert closed_session is not None
        assert closed_session.status == "closed"

    # audit trails: actions + transitions + score datapoints were all stamped.
    assert _count(factory, PlanningSessionAction, sid) > 0
    assert _count(factory, PlanningPhaseTransition, sid) > 0
    assert _count(factory, PlanningEntityScoreDatapoint, sid) > 0


def test_decomposition_gate_tracks_crucible_result_passed(tmp_path: Path) -> None:
    """The corrected adapter: a binary phase's ``gate_result`` follows ``result.passed``.

    crucible SKIPS the scoring layer at ``epic_decomposition`` (its verdict lives in the
    structural / cross-validation layers, surfaced as the top-level ``result.passed``). The
    old adapter read only ``scoring.gate_result`` and so folded that phase to ``'fail'`` for
    ANY content. The fixed adapter reads ``result.passed`` — so a structurally-complete
    decomposition now PASSES (despite skipped scoring) while an incomplete one FAILS.
    """
    adapter = CrucibleValidatorAdapter()

    complete_factory = _open(tmp_path / "complete")
    complete_id = _seed(complete_factory)
    with complete_factory.begin() as session:
        complete_full = SqliteSpecStore(session).get_spec_full(complete_id)
        assert complete_full is not None
        complete_config = resolve_validator_config(
            ConfigStoreSqlite(session), PROJECT_ID, complete_id
        )
        complete_dict = complete_full.spec.model_dump(by_alias=True, exclude_none=True)

    # scoring is genuinely SKIPPED here — yet the corrected gate PASSES (tracks result.passed).
    raw = crucible.validate(
        complete_dict,
        {"phase": "epic_decomposition", "config": complete_config, "activeEntityId": None,
         "returns": ["structural", "scoring", "guidance"]},
    )
    assert isinstance(raw, ValidationResult)
    assert raw.passed is True
    assert not isinstance(raw.scoring, ScoringResultActive)  # scoring skipped
    assert adapter.validate(
        complete_full, PlanningPhase.EPIC_DECOMPOSITION, complete_config
    ).gate_result == "pass"

    # an under-decomposed spec (one epic < the min-count of 2) FAILS the same phase.
    thin_factory = _open(tmp_path / "thin")
    thin_id = _seed_thin(thin_factory, epics=1)
    with thin_factory.begin() as session:
        thin_full = SqliteSpecStore(session).get_spec_full(thin_id)
        assert thin_full is not None
        thin_config = resolve_validator_config(ConfigStoreSqlite(session), PROJECT_ID, thin_id)
    assert adapter.validate(
        thin_full, PlanningPhase.EPIC_DECOMPOSITION, thin_config
    ).gate_result == "fail"


# --------------------------------------------------------------------------------------
# one-active-session lock (a racing 2nd start_planning_session -> CONFLICT)
# --------------------------------------------------------------------------------------


def test_one_active_session_lock_yields_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``ix_one_active_planning_session`` partial-unique lock holds.

    A second SPS that *sees* the active session resumes idempotently; the lock only
    bites in the concurrency window where the racing session is not yet visible. That
    race is reproduced by forcing the create path (``get_active...`` returns ``None``)
    while a real active session exists — the INSERT hits the unique index → ``CONFLICT``.
    """
    factory = _open(tmp_path)
    spec_id = _seed(factory)
    dispatcher = make_dispatcher(factory)

    dispatcher.dispatch("start_planning_session", {"specId": spec_id})  # one active session
    assert _spec_status(factory, spec_id) == "planning"

    # Simulate the race: the concurrent active session is not yet visible to the 2nd call.
    monkeypatch.setattr(
        lifecycle_ports_module.SqlitePlanningSessionStore,
        "get_active_planning_session_by_spec",
        lambda self, spec: None,
    )
    conflict = dispatcher.dispatch("start_planning_session", {"specId": spec_id})

    assert conflict["kind"] == "standard_error"
    assert conflict["code"] == "CONFLICT"
    assert conflict["guidance"]["prose"]  # non-empty English prose
    assert conflict["guidance"]["next_actions"]  # at least one concrete next step

    # The lock held: still exactly one non-closed session for the spec.
    with factory.begin() as session:
        active = session.execute(
            select(func.count())
            .select_from(PlanningSession)
            .where(
                PlanningSession.specification_id == spec_id,
                PlanningSession.status != "closed",
            )
        ).scalar_one()
        assert active == 1


# --------------------------------------------------------------------------------------
# error shapes: NOT_FOUND / PRECONDITION_FAILED carry English guidance
# --------------------------------------------------------------------------------------


def test_get_missing_specification_is_not_found_with_english_guidance(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    _seed(factory)

    result = make_dispatcher(factory).dispatch("get", {"type": "specification", "id": MISSING_ID})

    assert result["kind"] == "standard_error"
    assert result["code"] == "NOT_FOUND"
    prose = result["guidance"]["prose"]
    assert isinstance(prose, str) and prose  # canonical, agent-directed English
    assert result["guidance"]["next_actions"]  # at least one concrete next step


def test_reopen_non_ready_spec_is_precondition_failed(tmp_path: Path) -> None:
    factory = _open(tmp_path)
    spec_id = _seed(factory)  # a fresh seed is `draft`, not `ready`

    result = make_dispatcher(factory).dispatch(
        "reopen_specification", {"specificationId": spec_id}
    )

    assert result["kind"] == "standard_error"
    assert result["code"] == "PRECONDITION_FAILED"
    assert result["guidance"]["prose"]
    assert result["guidance"]["next_actions"]


def test_approve_handover_on_failing_gate_is_precondition_failed(tmp_path: Path) -> None:
    """The other PRECONDITION_FAILED path: approve while the phase gate is failing."""
    factory = _open(tmp_path)
    spec_id = _seed(factory)
    dispatcher = make_dispatcher(factory)
    dispatcher.dispatch("start_planning_session", {"specId": spec_id})
    sid = _session_id(factory, spec_id)

    # Park the session for review but with a FAILING last gate result (the M11.1 guard).
    _set_session(factory, sid, status="awaiting_human_review", last_gate_result="fail")
    result = dispatcher.dispatch("approve_handover", {"sessionId": sid, "userId": "dev"})

    assert result["kind"] == "standard_error"
    assert result["code"] == "PRECONDITION_FAILED"
    assert result["guidance"]["prose"]
    assert result["guidance"]["next_actions"]
