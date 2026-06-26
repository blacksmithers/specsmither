"""L7 acceptance for the read-only report primitives (work item #21).

Exercises the surviving report kinds against a real SQLite database with an
INJECTED clock, asserting the load-bearing behaviours:

1. **Velocity / ±10% band** — :func:`velocity_per_day` is a trailing 7-day rate
   and :func:`velocity_trend` bands at ±10%; the rate also surfaces end-to-end in
   :func:`implementation_summary`.
2. **Estimate drift** — flags an epic whose stored estimate diverges from the live
   ticket-sum by ``>20%`` and ignores a ``<20%`` case (the TS threshold).
3. **Dropped reports** — ``work`` / ``implementation_analysis`` raise.
4. **Readiness degrades** — under NO-LLM the AI score fields are ``None`` and the
   score-driven attention items are empty, with no crash; the deterministic
   categories (file conflicts, dependency health, estimate drift) still compute.

Plus dashboard (materialized-column counts + blocker), blockers (blocker depth)
and the planning-only active-sessions dashboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import Epic as EpicModel
from specsmither.db.models import PlanningSession
from specsmither.db.models import Ticket as TicketModel
from specsmither.db.repositories import AllStores, ProjectRecord, make_stores
from specsmither.domain.enums import TicketStatus
from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord
from specsmither.operations.errors import NotFoundError
from specsmither.operations.reports import (
    active_sessions,
    blockers_report,
    dashboard_report,
    estimate_drift,
    implementation_analysis_report,
    implementation_summary,
    readiness_report,
    velocity_per_day,
    velocity_trend,
    work_report,
)
from specsmither.rollups.recompute import recompute

PROJECT_ID = "proj-1"
SPEC_ID = "spec-1"
EPIC_ID = "epic-1"
EPIC2_ID = "epic-2"

NOW = datetime(2026, 6, 26, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# fixtures / seeding                                                          #
# --------------------------------------------------------------------------- #


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    return make_session_factory(init_db(tmp_path / "reports.db"))


def _seed_spine(stores: AllStores, *, with_second_epic: bool = False) -> None:
    stores.projects.create_project(ProjectRecord(id=PROJECT_ID, name="P"))
    stores.specifications.create_specification(
        SpecificationRecord(id=SPEC_ID, project_id=PROJECT_ID, title="S")
    )
    stores.epics.create_epic(
        EpicRecord(
            id=EPIC_ID,
            specification_id=SPEC_ID,
            title="Epic One",
            description="d",
            objective="o",
            epic_number=1,
            order=0,
        )
    )
    if with_second_epic:
        stores.epics.create_epic(
            EpicRecord(
                id=EPIC2_ID,
                specification_id=SPEC_ID,
                title="Epic Two",
                description="d",
                objective="o",
                epic_number=2,
                order=1,
            )
        )


def _ticket(
    ticket_id: str,
    *,
    epic_id: str = EPIC_ID,
    number: int,
    status: TicketStatus = TicketStatus.PENDING,
    estimated: int | None = None,
    files_modified: list[str] | None = None,
) -> TicketRecord:
    return TicketRecord(
        id=ticket_id,
        epic_id=epic_id,
        title=f"Ticket {number}",
        ticket_number=number,
        status=status,
        estimated_minutes=estimated,
        files_to_be_modified=files_modified or [],
    )


def _set_epic_estimate(session: Session, epic_id: str, minutes: int) -> None:
    """Stamp a *stored* epic estimate directly (bypassing the recompute equaliser)."""
    epic = session.get(EpicModel, epic_id)
    assert epic is not None
    epic.estimated_minutes = minutes


def _done(ticket_id: str, updated_at: str) -> TicketRecord:
    """A standalone ``done`` ticket with a fixed ``updated_at`` (for velocity)."""
    return TicketRecord(
        id=ticket_id,
        epic_id=EPIC_ID,
        title=ticket_id,
        ticket_number=0,
        status=TicketStatus.DONE,
        updated_at=updated_at,
    )


# --------------------------------------------------------------------------- #
# 1. velocity: 7-day window + ±10% band                                       #
# --------------------------------------------------------------------------- #


def test_velocity_per_day_is_a_trailing_seven_day_rate() -> None:
    # 7 done tickets inside the trailing 7 days; 2 older ones outside it.
    inside = [_done(f"in-{i}", "2026-06-22T00:00:00+00:00") for i in range(7)]
    outside = [_done(f"out-{i}", "2026-06-01T00:00:00+00:00") for i in range(2)]
    # A non-done ticket dated recently must NOT count toward velocity.
    not_done = TicketRecord(
        id="active",
        epic_id=EPIC_ID,
        title="active",
        ticket_number=0,
        status=TicketStatus.ACTIVE,
        updated_at="2026-06-25T00:00:00+00:00",
    )

    assert velocity_per_day([*inside, *outside, not_done], NOW) == 7 / 7
    assert velocity_per_day(outside, NOW) == 0.0


def test_velocity_trend_bands_at_plus_minus_ten_percent() -> None:
    # > +10% -> increasing
    assert velocity_trend(12, 10) == "increasing"
    # < -10% -> decreasing
    assert velocity_trend(8, 10) == "decreasing"
    # equal -> stable
    assert velocity_trend(10, 10) == "stable"
    # exactly on the band edges -> stable (strict comparisons)
    assert velocity_trend(11, 10) == "stable"  # 11 is not > 11.0
    assert velocity_trend(9, 10) == "stable"  # 9 is not < 9.0
    assert velocity_trend(0, 0) == "stable"


def test_implementation_summary_surfaces_velocity_and_band(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores)
        # 3 done this week, 1 done last week -> 3 > 1*1.1 -> increasing.
        for i in range(3):
            stores.tickets.create_ticket(
                _ticket(f"tw-{i}", number=i, status=TicketStatus.DONE)
            )
        stores.tickets.create_ticket(_ticket("lw-0", number=10, status=TicketStatus.DONE))

    # Pin updated_at on the done tickets to land them in the two windows.
    with factory.begin() as session:
        for i in range(3):
            this_week = session.get(TicketModel, f"tw-{i}")
            assert this_week is not None
            this_week.updated_at = "2026-06-22T00:00:00+00:00"
        last_week = session.get(TicketModel, "lw-0")
        assert last_week is not None
        last_week.updated_at = "2026-06-16T00:00:00+00:00"

    report = implementation_summary(factory, now=NOW, specification_id=SPEC_ID)
    assert report.velocity.tickets_per_day == round(3 / 7, 2)
    assert report.velocity.trend == "increasing"
    assert report.overall.completed_tickets == 4


# --------------------------------------------------------------------------- #
# 2. estimate drift: >20% flagged, <20% ignored                               #
# --------------------------------------------------------------------------- #


def test_estimate_drift_flags_over_threshold_and_ignores_under(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores, with_second_epic=True)
        # Each epic's tickets sum to 100 estimated minutes.
        stores.tickets.create_ticket(
            _ticket("a-1", epic_id=EPIC_ID, number=1, estimated=100)
        )
        stores.tickets.create_ticket(
            _ticket("b-1", epic_id=EPIC2_ID, number=2, estimated=100)
        )

    # Stamp divergent *stored* epic estimates directly (bypassing recompute, which
    # would otherwise equalise epic.estimated_minutes to the ticket sum).
    with factory.begin() as session:
        _set_epic_estimate(session, EPIC_ID, 200)  # 50% drift -> flagged
        _set_epic_estimate(session, EPIC2_ID, 110)  # ~9.1% drift -> ignored

    drift = estimate_drift(factory, SPEC_ID, now=NOW)
    assert [d.epic_id for d in drift] == [EPIC_ID]
    assert drift[0].epic_estimated_minutes == 200
    assert drift[0].ticket_sum_minutes == 100
    assert drift[0].mismatch_percent == 50.0


def test_estimate_drift_unknown_spec_raises(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        _seed_spine(make_stores(session))
    with pytest.raises(NotFoundError):
        estimate_drift(factory, "missing", now=NOW)


# --------------------------------------------------------------------------- #
# 3. dropped reports raise                                                     #
# --------------------------------------------------------------------------- #


def test_dropped_reports_raise() -> None:
    with pytest.raises(NotImplementedError):
        work_report()
    with pytest.raises(NotImplementedError):
        implementation_analysis_report()


# --------------------------------------------------------------------------- #
# 4. readiness degrades gracefully (NO-LLM)                                    #
# --------------------------------------------------------------------------- #


def test_readiness_report_degrades_without_ai_scores(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores)
        # Two tickets touch the same file -> a file conflict (a non-AI category).
        stores.tickets.create_ticket(
            _ticket("t-1", number=1, files_modified=["src/shared.py"])
        )
        stores.tickets.create_ticket(
            _ticket("t-2", number=2, files_modified=["src/shared.py"])
        )

    report = readiness_report(factory, SPEC_ID, now=NOW)

    # AI scores degrade to null without crashing.
    assert report.spec_readiness.readiness_score is None
    assert report.spec_readiness.readiness_level is None
    assert report.epic_breakdown[0].avg_score is None
    assert all(ts.score is None for ts in report.epic_breakdown[0].ticket_scores)
    assert report.epic_breakdown[0].attention_items == []

    # The deterministic file-conflict category still computes.
    conflicts = {fc.file_path for fc in report.file_conflicts}
    assert "src/shared.py" in conflicts
    assert any(ap.category == "file_conflict" for ap in report.attention_points)
    # No low_readiness attention points (no scores to fall below threshold).
    assert all(ap.category != "low_readiness" for ap in report.attention_points)


def test_readiness_report_includes_estimate_drift(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores)
        stores.tickets.create_ticket(_ticket("t-1", number=1, estimated=100))
    with factory.begin() as session:
        _set_epic_estimate(session, EPIC_ID, 200)  # 50% drift

    report = readiness_report(factory, SPEC_ID, now=NOW)
    assert [d.epic_id for d in report.estimate_health] == [EPIC_ID]
    assert any(ap.category == "estimate_mismatch" for ap in report.attention_points)


# --------------------------------------------------------------------------- #
# 5. dashboard reads materialized counts + surfaces a blocker                  #
# --------------------------------------------------------------------------- #


def _seed_blocked(factory: sessionmaker[Session]) -> None:
    """A: active blocker; B: ready->demoted-to-pending (blocked) depending on A."""
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores)
        stores.tickets.create_ticket(
            _ticket("A", number=1, status=TicketStatus.ACTIVE, estimated=60)
        )
        stores.tickets.create_ticket(
            _ticket("B", number=2, status=TicketStatus.READY, estimated=30)
        )
        stores.ticket_dependencies.add_dependency("B", "A")
        # Recompute materialises the count columns + demotes B (ready -> pending,
        # stamping a block_reason because A is not done).
        recompute(session, spec_ids=[SPEC_ID], generated_at=NOW.isoformat())


def test_dashboard_reads_materialized_columns_and_blocker(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed_blocked(factory)

    report = dashboard_report(factory, SPEC_ID, now=NOW)
    overall = report.overall_progress
    assert overall.total == 2
    assert overall.completed == 0
    assert overall.in_progress == 1  # A is active
    assert overall.pending == 1  # B demoted to pending
    assert overall.failed == 0  # no 'failed' status in SpecSmither
    assert overall.blocked == 1  # B carries a block_reason

    # The blocker surfaces with its first non-done dependency (A).
    assert len(report.blockers) == 1
    blocker = report.blockers[0]
    assert blocker.ticket.id == "B"
    assert blocker.blocked_by is not None
    assert blocker.blocked_by.id == "A"
    assert blocker.blocked_by_status == "active"

    # estimated_minutes is read from the materialized spec column.
    assert report.time_metrics.estimated_minutes == 90


def test_blockers_report_lists_blocker_depth(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    _seed_blocked(factory)

    report = blockers_report(factory, now=NOW, specification_id=SPEC_ID)
    assert report.summary.total_blocked == 1
    entry = report.blockers[0]
    assert entry.ticket.id == "B"
    # The blocking dependency (A, still active) is reported as blocker depth.
    assert [d.ticket_id for d in entry.dependencies] == ["A"]
    assert entry.dependencies[0].status == "active"
    assert report.by_specification[0].blocked_count == 1


# --------------------------------------------------------------------------- #
# 6. active sessions (planning only)                                          #
# --------------------------------------------------------------------------- #


def test_active_sessions_reports_planning_only(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        stores = make_stores(session)
        _seed_spine(stores)
        session.add(
            PlanningSession(
                specification_id=SPEC_ID,
                status="active",
                current_phase="planning_spec",
                started_at=NOW.isoformat(),
                actions_count=3,
            )
        )

    report = active_sessions(factory, PROJECT_ID, now=NOW)
    assert report.summary.total_active == 1
    assert report.summary.by_type.planning == 1
    assert report.summary.by_type.work == 0
    assert report.work == []
    assert len(report.planning) == 1
    assert report.planning[0].spec_title == "S"
    assert report.planning[0].actions_count == 3
