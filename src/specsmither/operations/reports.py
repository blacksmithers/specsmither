"""Read-only report primitives.

The report read surface, built over the SQLite stores. This is the *primitive*
layer: the individual report builders. The MCP ``get``/``list`` composers and the
``getReport`` dispatch facade are a 0.1.0 concern and are deliberately out of
scope — these functions are the building blocks that facade will route to.

Two cross-cutting decisions carry the whole module:

* **Injected clock (determinism).** Every report function takes a ``now``
  (:data:`Clock` — a ``datetime`` or a zero-arg callable returning one) and NEVER
  calls :func:`datetime.now`. All velocity windows, block durations and
  "estimated completion" dates are computed relative to the injected instant, so
  a fixed ``now`` makes every output reproducible and golden-testable.
* **Read-only, no recompute.** Reports never mutate; each opens a short-lived read
  session (``with session_factory() as session``) and reads through the stores. The
  in-transaction recompute worklist is a *mutation* contract — it does not run
  here. The denormalized count columns these reports read (``ticket_count`` /
  ``progress`` / per-status buckets / ``estimated_minutes``) are materialized by
  the mutation path's recompute, so a report trusts them as fresh.

Report kinds that survive in SpecSmither:

* :func:`dashboard_report` — counts/overview, read from the materialized columns,
  plus the live blocked/next-actionable/complexity scan.
* :func:`implementation_summary` — progress breakdown with the 7-day
  :func:`velocity_per_day` and the ±10% :func:`velocity_trend` band.
* :func:`time_report` — estimation-only sums (``actualMinutes`` is frozen-zone and
  absent, so efficiency/variance collapse out).
* :func:`blockers_report` — the blocked tickets + their immediate blocking
  dependencies (blocker depth), with injected-clock block durations.
* :func:`estimate_drift` — flags epics whose stored estimate diverges from the
  live ticket-sum by more than the ``>20`` percent threshold.
* :func:`readiness_report` — **degrades gracefully**: SpecSmither is NO-LLM, so the
  AI readiness score fields do not exist; they surface as ``None`` and the
  score-driven attention items are simply empty. The non-AI categories (file
  conflicts, dependency health, estimate drift) are computed for real.
* :func:`active_sessions` — active **planning** sessions only (work/review are
  frozen-zone, always empty in v0.1.0).

Dropped (AI / frozen-zone): :func:`work_report` and
:func:`implementation_analysis_report` raise :class:`NotImplementedError` — there
is no work lifecycle / record-events store in SpecSmither M0.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NoReturn

from specsmither.db.repositories import make_stores
from specsmither.domain.enums import PlanningSessionStatus, SpecStatus, TicketStatus
from specsmither.operations.errors import NotFoundError, ValidationFailedError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.db.repositories import AllStores
    from specsmither.domain.records import DependencyEdge, TicketRecord

__all__ = [
    "ActiveSession",
    "ActiveSessions",
    "AttentionPoint",
    "BlockInfo",
    "BlockerDependency",
    "BlockerEntry",
    "BlockerScope",
    "BlockerTicket",
    "BlockersReport",
    "BlockersSummary",
    "ByEpicBlock",
    "BySpecificationBlock",
    "Clock",
    "ComplexityDistribution",
    "DashboardBlocker",
    "DashboardReport",
    "DependencyHealth",
    "EntityRef",
    "EpicProgress",
    "EpicReadinessBreakdown",
    "EstimateDriftEntry",
    "FailedTicket",
    "FileConflict",
    "ImplementationBlocker",
    "ImplementationForecast",
    "ImplementationOverall",
    "ImplementationSpec",
    "ImplementationSummary",
    "LongestBlocked",
    "NextActionable",
    "OverallProgress",
    "PriorityDistribution",
    "ProjectRef",
    "ReadinessReport",
    "SessionsByType",
    "SessionsSummary",
    "SpecReadiness",
    "SpecSummaryLine",
    "TicketRef",
    "TicketScore",
    "TimeByEpic",
    "TimeByTicket",
    "TimeReport",
    "TimeScope",
    "TimeSummary",
    "Velocity",
    "active_sessions",
    "blockers_report",
    "dashboard_report",
    "estimate_drift",
    "implementation_analysis_report",
    "implementation_summary",
    "readiness_report",
    "time_report",
    "velocity_per_day",
    "velocity_trend",
    "work_report",
]

#: A clock: either a concrete instant or a zero-arg callable producing one. Report
#: functions accept this and never read the wall clock directly.
Clock = Callable[[], datetime] | datetime

#: The velocity / trend window (a fixed 7-day rolling window).
_WINDOW = timedelta(days=7)


# --------------------------------------------------------------------------- #
# clock / time helpers                                                         #
# --------------------------------------------------------------------------- #


def _resolve_now(now: Clock) -> datetime:
    """Resolve the injected clock to a timezone-aware UTC instant."""
    moment = now() if callable(now) else now
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime (or ``None`` if unparseable).

    Tolerates bad input: a missing/garbage timestamp yields ``None`` and is treated
    as "not in the window" by callers.
    """
    if not value:
        return None
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _round_half_up(value: float) -> int:
    """Round to the nearest int, halves toward +inf (matches JS ``Math.round``)."""
    return math.floor(value + 0.5)


def _round2(value: float) -> float:
    """Round to two decimals, halves toward +inf (matches ``Math.round(x*100)/100``)."""
    return math.floor(value * 100 + 0.5) / 100


def _format_duration(minutes: int) -> str:
    """Format minutes → ``"5h"`` / ``"2d 3h"``."""
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def _calculate_duration(since: str | None, moment: datetime) -> str:
    """Elapsed ``moment - since`` → ``"5h"`` / ``"2d 3h"``."""
    start = _parse_iso(since) or moment
    hours = math.floor((moment - start).total_seconds() / 3600)
    days = hours // 24
    if days > 0:
        return f"{days}d {hours % 24}h"
    return f"{hours}h"


def _duration_minutes(since: str | None, moment: datetime) -> int:
    """Block duration in minutes (``round((now - updatedAt) / 60000)``)."""
    start = _parse_iso(since) or moment
    return _round_half_up((moment - start).total_seconds() / 60.0)


def _est(value: int | None) -> int:
    """Estimated minutes, coalescing ``None`` to ``0``."""
    return value if value is not None else 0


# --------------------------------------------------------------------------- #
# velocity (pure, 7-day window + ±10% trend band)                             #
# --------------------------------------------------------------------------- #


def _count_done_in_window(
    tickets: Sequence[TicketRecord],
    *,
    after: datetime,
    before: datetime | None = None,
) -> int:
    """Count ``done`` tickets whose ``updated_at`` falls in ``(after, before]``.

    SpecSmither has no ``completed_at`` column, so ``updated_at`` is the completion
    proxy.
    """
    count = 0
    for ticket in tickets:
        if ticket.status != TicketStatus.DONE:
            continue
        moment = _parse_iso(ticket.updated_at)
        if moment is None or moment <= after:
            continue
        if before is not None and moment > before:
            continue
        count += 1
    return count


def velocity_per_day(tickets: Sequence[TicketRecord], moment: datetime) -> float:
    """Tickets/day over the trailing 7-day window: ``recentDone / 7``.

    ``recentDone`` = ``done`` tickets completed (``updated_at``) within 7 days of
    ``moment``. This is the raw rate (no rounding); callers round for display.
    """
    recent = _count_done_in_window(tickets, after=moment - _WINDOW)
    return recent / 7


def velocity_trend(this_week: int, last_week: int) -> str:
    """The ±10% banded trend: ``increasing`` / ``decreasing`` / ``stable``.

    ``increasing`` when ``this_week > last_week * 1.1``; ``decreasing`` when
    ``this_week < last_week * 0.9``; otherwise ``stable`` (inside the ±10% band).
    """
    if this_week > last_week * 1.1:
        return "increasing"
    if this_week < last_week * 0.9:
        return "decreasing"
    return "stable"


def _confidence(rate: float) -> str:
    """Forecast confidence: ``>1`` high, ``>0.5`` medium, else low."""
    if rate > 1:
        return "high"
    if rate > 0.5:
        return "medium"
    return "low"


def _deps_by_ticket(edges: Sequence[DependencyEdge]) -> dict[str, list[str]]:
    """Map each dependent ticket id to the ids it depends on (its targets)."""
    out: dict[str, list[str]] = {}
    for edge in edges:
        out.setdefault(edge.ticket_id, []).append(edge.depends_on_id)
    return out


# =========================================================================== #
# result shapes                                                               #
# =========================================================================== #


@dataclass(frozen=True, slots=True)
class EntityRef:
    """A minimal ``{id, title}`` reference (tickets in conflicts / orphan lists)."""

    id: str
    title: str


@dataclass(frozen=True, slots=True)
class TicketRef:
    """A ``{id, ticketNumber, title}`` ticket reference."""

    id: str
    ticket_number: int
    title: str


# --- dashboard -------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OverallProgress:
    """Dashboard top-line counts (read from the materialized spec columns)."""

    total: int
    completed: int
    in_progress: int
    pending: int
    failed: int
    blocked: int
    percent_complete: int


@dataclass(frozen=True, slots=True)
class TimeMetrics:
    """Dashboard estimate/velocity block."""

    estimated_minutes: int
    remaining_minutes: int
    velocity_per_day: float
    estimated_completion: str


@dataclass(frozen=True, slots=True)
class SpecSummaryLine:
    """The single-spec progress line in the dashboard ``specifications`` array."""

    id: str
    title: str
    completed: int
    total: int
    percent_complete: int


@dataclass(frozen=True, slots=True)
class EpicProgress:
    """Per-epic progress line (from the materialized epic columns)."""

    id: str
    code: str
    title: str
    completed: int
    total: int
    percent_complete: int


@dataclass(frozen=True, slots=True)
class PriorityDistribution:
    """Hardcoded priority buckets (no priority field in the planning era)."""

    high: int
    medium: int
    low: int


@dataclass(frozen=True, slots=True)
class ComplexityDistribution:
    """Complexity buckets over SpecSmither's ``Complexity`` vocabulary.

    Crucible's four-value ``small/medium/large/xlarge`` vocabulary; ``medium`` also
    catches a missing complexity.
    """

    small: int
    medium: int
    large: int
    xlarge: int


@dataclass(frozen=True, slots=True)
class NextActionable:
    """A pending/ready ticket whose dependencies are all complete."""

    id: str
    ticket_number: int
    title: str
    priority: str
    complexity: str
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class DashboardBlocker:
    """A blocked ticket + its first non-done blocking dependency (if any)."""

    ticket: TicketRef
    blocked_by: TicketRef | None
    blocked_by_status: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class FailedTicket:
    """A failed ticket (always empty in SpecSmither — no ``failed`` status)."""

    ticket: TicketRef
    reason: str | None


@dataclass(frozen=True, slots=True)
class DashboardReport:
    """The full dashboard view for one specification."""

    overall_progress: OverallProgress
    time_metrics: TimeMetrics
    specifications: list[SpecSummaryLine]
    epics: list[EpicProgress]
    priority_distribution: PriorityDistribution
    complexity_distribution: ComplexityDistribution
    next_actionable: list[NextActionable]
    blockers: list[DashboardBlocker]
    failed_tickets: list[FailedTicket]
    updated_at: str


# --- implementation summary ------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A ``{id, name}`` project reference."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Velocity:
    """Project velocity: tickets/day + the ±10% banded trend."""

    tickets_per_day: float
    trend: str


@dataclass(frozen=True, slots=True)
class ImplementationOverall:
    """Aggregate counts across the implementation scope."""

    total_specifications: int
    in_progress_specifications: int
    total_tickets: int
    completed_tickets: int
    in_progress_tickets: int
    blocked_tickets: int
    overall_progress: int


@dataclass(frozen=True, slots=True)
class ImplementationSpec:
    """Per-spec progress + forecast line."""

    id: str
    title: str
    status: str
    progress: int
    tickets_remaining: int
    estimated_completion: str | None
    blocker_count: int


@dataclass(frozen=True, slots=True)
class ImplementationBlocker:
    """A blocked ticket with its spec context + injected-clock duration."""

    specification_id: str
    specification_title: str
    ticket_id: str
    ticket_title: str
    reason: str
    duration: str


@dataclass(frozen=True, slots=True)
class ImplementationForecast:
    """Whole-scope completion forecast."""

    all_specs_complete: str | None
    confidence: str


@dataclass(frozen=True, slots=True)
class ImplementationSummary:
    """The implementation progress report (project- or spec-scoped)."""

    project: ProjectRef
    overall: ImplementationOverall
    velocity: Velocity
    specifications: list[ImplementationSpec]
    blockers: list[ImplementationBlocker]
    forecast: ImplementationForecast


# --- time report ------------------------------------------------------------ #


@dataclass(frozen=True, slots=True)
class TimeScope:
    """The resolved scope of a time report."""

    type: str
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class TimeSummary:
    """Estimation-only summary (actuals are frozen-zone)."""

    total_estimated_minutes: int


@dataclass(frozen=True, slots=True)
class TimeByEpic:
    """Per-epic estimated-minutes roll-up."""

    id: str
    title: str
    specification_id: str
    estimated_minutes: int
    ticket_count: int


@dataclass(frozen=True, slots=True)
class TimeByTicket:
    """Per-ticket estimate line."""

    id: str
    title: str
    epic_id: str
    status: str
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class TimeReport:
    """Estimation-only time report (project/spec/epic scope)."""

    scope: TimeScope
    summary: TimeSummary
    by_epic: list[TimeByEpic]
    by_ticket: list[TimeByTicket]


# --- blockers report -------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BlockerScope:
    """The resolved scope of a blockers report."""

    type: str
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class BlockerTicket:
    """The blocked ticket itself."""

    id: str
    title: str
    ticket_number: int
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class BlockInfo:
    """The block reason + injected-clock duration."""

    reason: str
    updated_at: str
    duration_minutes: int
    duration_formatted: str


@dataclass(frozen=True, slots=True)
class BlockerDependency:
    """One non-done dependency keeping a ticket blocked (blocker depth)."""

    ticket_id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class BlockerEntry:
    """A blocked ticket with its epic/spec context and blocking dependencies."""

    ticket: BlockerTicket
    epic: EntityRef
    specification: EntityRef
    block_info: BlockInfo
    dependencies: list[BlockerDependency]


@dataclass(frozen=True, slots=True)
class BySpecificationBlock:
    """Per-spec blocked roll-up."""

    id: str
    title: str
    blocked_count: int
    total_blocked_minutes: int


@dataclass(frozen=True, slots=True)
class ByEpicBlock:
    """Per-epic blocked roll-up."""

    id: str
    title: str
    specification_id: str
    blocked_count: int
    total_blocked_minutes: int


@dataclass(frozen=True, slots=True)
class LongestBlocked:
    """A top-5 longest-blocked entry."""

    ticket_id: str
    title: str
    duration_minutes: int
    reason: str


@dataclass(frozen=True, slots=True)
class BlockersSummary:
    """Blockers aggregate summary."""

    total_blocked: int
    total_blocked_minutes: int
    average_block_duration: int
    oldest_blocker: str | None


@dataclass(frozen=True, slots=True)
class BlockersReport:
    """The blockers report (project- or spec-scoped)."""

    scope: BlockerScope
    summary: BlockersSummary
    blockers: list[BlockerEntry]
    by_specification: list[BySpecificationBlock]
    by_epic: list[ByEpicBlock]
    longest_blocked: list[LongestBlocked]


# --- estimate drift --------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EstimateDriftEntry:
    """An epic whose stored estimate diverges from its ticket-sum by ``>20%``."""

    epic_id: str
    epic_title: str
    epic_estimated_minutes: int
    ticket_sum_minutes: int
    mismatch_percent: float


# --- readiness (degraded, NO-LLM) ------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SpecReadiness:
    """Spec-level readiness — all AI scores are ``None`` under NO-LLM."""

    specification_id: str
    readiness_score: float | None
    readiness_level: str | None
    global_readiness_score: float | None
    global_readiness_level: str | None


@dataclass(frozen=True, slots=True)
class TicketScore:
    """A ticket's readiness score (always ``None`` under NO-LLM)."""

    id: str
    title: str
    score: float | None
    level: str | None


@dataclass(frozen=True, slots=True)
class EpicReadinessBreakdown:
    """Per-epic readiness breakdown — scores degrade to ``None``/empty."""

    epic_id: str
    epic_title: str
    avg_score: float | None
    global_readiness_score: float | None
    global_readiness_level: str | None
    ticket_count: int
    ticket_scores: list[TicketScore]
    attention_items: list[TicketScore]


@dataclass(frozen=True, slots=True)
class FileConflict:
    """A file modified by more than one ticket, with a dependency-edge check."""

    file_path: str
    tickets: list[EntityRef]
    has_dependency: bool


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    """Dependency-graph health: undirected dependency count + orphan tickets."""

    total_dependencies: int
    orphan_tickets: list[EntityRef]


@dataclass(frozen=True, slots=True)
class AttentionPoint:
    """A surfaced readiness concern (file conflict / estimate mismatch)."""

    severity: str
    category: str
    message: str
    entity_id: str
    entity_title: str


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """The readiness report — AI categories degrade, non-AI categories are real."""

    spec_readiness: SpecReadiness
    epic_breakdown: list[EpicReadinessBreakdown]
    file_conflicts: list[FileConflict]
    dependency_health: DependencyHealth
    estimate_health: list[EstimateDriftEntry]
    attention_points: list[AttentionPoint]


# --- sessions dashboard ----------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """An active planning session line."""

    session_id: str
    specification_id: str
    spec_title: str | None
    current_phase: str
    started_at: str | None
    actions_count: int


@dataclass(frozen=True, slots=True)
class SessionsByType:
    """Active-session counts by lifecycle type (work/review always 0 in M0)."""

    planning: int
    work: int
    review: int


@dataclass(frozen=True, slots=True)
class SessionsSummary:
    """Active-sessions summary."""

    total_active: int
    by_type: SessionsByType


@dataclass(frozen=True, slots=True)
class ActiveSessions:
    """The active-sessions dashboard for a project (planning only in M0)."""

    planning: list[ActiveSession]
    work: list[ActiveSession]
    review: list[ActiveSession]
    summary: SessionsSummary


# =========================================================================== #
# scope helpers                                                               #
# =========================================================================== #


def _spec_tickets(stores: AllStores, spec_id: str) -> list[TicketRecord]:
    """Every ticket in a spec (flattened across its epics)."""
    tickets: list[TicketRecord] = []
    for epic in stores.epics.list_epics(specification_id=spec_id):
        tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
    return tickets


# =========================================================================== #
# dashboard                                                                   #
# =========================================================================== #


def dashboard_report(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    now: Clock,
) -> DashboardReport:
    """Build the dashboard for ``specification_id``.

    Counts/overview come from the materialized spec/epic columns (kept fresh by the
    mutation-path recompute); the blocked list, complexity distribution, velocity
    and next-actionable set are scanned live from the spec's tickets.
    """
    if not specification_id:
        raise ValidationFailedError("specificationId is required for a dashboard report.")
    moment = _resolve_now(now)

    with session_factory() as session:
        stores = make_stores(session)
        spec = stores.specifications.get_specification(specification_id)
        epics = stores.epics.list_epics(specification_id=specification_id)

        all_tickets: list[TicketRecord] = []
        for epic in epics:
            all_tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
        deps_by = _deps_by_ticket(
            stores.ticket_dependencies.list_dependencies(specification_id=specification_id)
        )

    completed_ids = {t.id for t in all_tickets if t.status == TicketStatus.DONE}
    ticket_by_id = {t.id: t for t in all_tickets}

    # --- counts / overview from the materialized columns ---
    total = spec.ticket_count
    completed = spec.completed_ticket_count
    blocked = sum(1 for t in all_tickets if t.block_reason is not None)
    overall = OverallProgress(
        total=total,
        completed=completed,
        in_progress=spec.active_ticket_count,
        pending=spec.pending_ticket_count + spec.ready_ticket_count,
        failed=0,
        blocked=blocked,
        percent_complete=spec.progress,
    )

    # --- time metrics ---
    estimated_minutes = _est(spec.estimated_minutes)
    completed_estimate = sum(
        _est(t.estimated_minutes) for t in all_tickets if t.status == TicketStatus.DONE
    )
    velocity = velocity_per_day(all_tickets, moment)
    remaining_count = total - completed
    days_remaining = remaining_count / velocity if velocity > 0 else 0.0
    estimated_completion = (
        (moment + timedelta(days=days_remaining)).date().isoformat()
        if days_remaining > 0
        else "Unknown"
    )
    time_metrics = TimeMetrics(
        estimated_minutes=estimated_minutes,
        remaining_minutes=estimated_minutes - completed_estimate,
        velocity_per_day=_round2(velocity),
        estimated_completion=estimated_completion,
    )

    # --- per-epic progress from the materialized epic columns ---
    epic_progress = [
        EpicProgress(
            id=epic.id,
            code=f"E{(epic.epic_number or 0):02d}",
            title=epic.title,
            completed=epic.completed_ticket_count,
            total=epic.ticket_count,
            percent_complete=epic.progress,
        )
        for epic in epics
    ]

    # --- distributions (live scan) ---
    priority_distribution = PriorityDistribution(high=0, medium=len(all_tickets), low=0)
    complexity_distribution = _complexity_distribution(all_tickets)

    # --- next actionable: first 10 pending/ready, dep-complete, capped at 5 ---
    next_actionable: list[NextActionable] = []
    pending_actionable = [
        t for t in all_tickets if t.status in (TicketStatus.PENDING, TicketStatus.READY)
    ]
    for ticket in pending_actionable[:10]:
        if len(next_actionable) >= 5:
            break
        targets = deps_by.get(ticket.id, [])
        if all(dep in completed_ids for dep in targets):
            next_actionable.append(
                NextActionable(
                    id=ticket.id,
                    ticket_number=ticket.ticket_number or 0,
                    title=ticket.title,
                    priority="medium",
                    complexity=ticket.complexity.value if ticket.complexity is not None else "medium",
                    estimated_minutes=_est(ticket.estimated_minutes),
                )
            )

    # --- blockers: first non-done dependency per blocked ticket ---
    blockers: list[DashboardBlocker] = []
    for ticket in (t for t in all_tickets if t.block_reason is not None):
        blocked_by: TicketRef | None = None
        blocked_by_status: str | None = None
        for dep_id in deps_by.get(ticket.id, []):
            dep = ticket_by_id.get(dep_id)
            if dep is not None and dep.status != TicketStatus.DONE:
                blocked_by = TicketRef(dep.id, dep.ticket_number or 0, dep.title)
                blocked_by_status = dep.status.value
                break
        blockers.append(
            DashboardBlocker(
                ticket=TicketRef(ticket.id, ticket.ticket_number or 0, ticket.title),
                blocked_by=blocked_by,
                blocked_by_status=blocked_by_status,
                reason=ticket.block_reason,
            )
        )

    return DashboardReport(
        overall_progress=overall,
        time_metrics=time_metrics,
        specifications=[
            SpecSummaryLine(
                id=spec.id,
                title=spec.title,
                completed=completed,
                total=total,
                percent_complete=spec.progress,
            )
        ],
        epics=epic_progress,
        priority_distribution=priority_distribution,
        complexity_distribution=complexity_distribution,
        next_actionable=next_actionable,
        blockers=blockers,
        failed_tickets=[],
        updated_at=moment.isoformat(),
    )


def _complexity_distribution(tickets: Sequence[TicketRecord]) -> ComplexityDistribution:
    """Bucket tickets by complexity (a missing complexity falls into ``medium``)."""
    small = medium = large = xlarge = 0
    for ticket in tickets:
        value = ticket.complexity.value if ticket.complexity is not None else None
        if value == "small":
            small += 1
        elif value == "large":
            large += 1
        elif value == "xlarge":
            xlarge += 1
        else:  # "medium" or missing
            medium += 1
    return ComplexityDistribution(small=small, medium=medium, large=large, xlarge=xlarge)


# =========================================================================== #
# implementation summary                                                      #
# =========================================================================== #


def implementation_summary(
    session_factory: sessionmaker[Session],
    *,
    now: Clock,
    project_id: str | None = None,
    specification_id: str | None = None,
) -> ImplementationSummary:
    """Progress breakdown for a project or a single specification.

    Carries the 7-day :func:`velocity_per_day` rate and the ±10%
    :func:`velocity_trend` band (the report's headline analytics).
    """
    if not project_id and not specification_id:
        raise ValidationFailedError("Either projectId or specificationId is required.")
    moment = _resolve_now(now)

    with session_factory() as session:
        stores = make_stores(session)
        if specification_id:
            spec = stores.specifications.get_specification(specification_id)
            resolved_project_id = spec.project_id
            specs = [spec]
            project_ref = _safe_project_ref(stores, resolved_project_id)
        else:
            assert project_id is not None  # mypy narrowing
            project = stores.projects.get_project(project_id)
            project_ref = ProjectRef(project.id, project.name)
            specs = stores.specifications.list_specifications(project_id=project_id)

        all_tickets: list[TicketRecord] = []
        spec_details: list[ImplementationSpec] = []
        ticket_spec: dict[str, tuple[str, str]] = {}
        for spec in specs:
            spec_tickets = _spec_tickets(stores, spec.id)
            all_tickets.extend(spec_tickets)
            for ticket in spec_tickets:
                ticket_spec[ticket.id] = (spec.id, spec.title)
            remaining = sum(1 for t in spec_tickets if t.status != TicketStatus.DONE)
            spec_velocity = velocity_per_day(spec_tickets, moment)
            days_remaining = remaining / spec_velocity if spec_velocity > 0 else None
            spec_details.append(
                ImplementationSpec(
                    id=spec.id,
                    title=spec.title,
                    status=spec.status.value,
                    progress=spec.progress,
                    tickets_remaining=remaining,
                    estimated_completion=(
                        (moment + timedelta(days=days_remaining)).isoformat()
                        if days_remaining is not None
                        else None
                    ),
                    blocker_count=sum(1 for t in spec_tickets if t.block_reason is not None),
                )
            )

    this_week = _count_done_in_window(all_tickets, after=moment - _WINDOW)
    last_week = _count_done_in_window(
        all_tickets, after=moment - 2 * _WINDOW, before=moment - _WINDOW
    )
    tickets_per_day = this_week / 7

    in_progress_specs = sum(
        1 for s in specs if s.status in (SpecStatus.IN_PROGRESS, SpecStatus.READY)
    )
    completed = sum(1 for t in all_tickets if t.status == TicketStatus.DONE)
    in_progress = sum(1 for t in all_tickets if t.status == TicketStatus.ACTIVE)
    blocked = sum(1 for t in all_tickets if t.block_reason is not None)

    blockers = [
        ImplementationBlocker(
            specification_id=ticket_spec.get(t.id, ("", ""))[0],
            specification_title=ticket_spec.get(t.id, ("", ""))[1],
            ticket_id=t.id,
            ticket_title=t.title,
            reason=t.block_reason or "No reason provided",
            duration=_calculate_duration(t.updated_at, moment),
        )
        for t in all_tickets
        if t.block_reason is not None
    ]

    total_remaining = sum(sd.tickets_remaining for sd in spec_details)
    overall_days_remaining = total_remaining / tickets_per_day if tickets_per_day > 0 else None

    return ImplementationSummary(
        project=project_ref,
        overall=ImplementationOverall(
            total_specifications=len(specs),
            in_progress_specifications=in_progress_specs,
            total_tickets=len(all_tickets),
            completed_tickets=completed,
            in_progress_tickets=in_progress,
            blocked_tickets=blocked,
            overall_progress=(
                _round_half_up(completed / len(all_tickets) * 100) if all_tickets else 0
            ),
        ),
        velocity=Velocity(
            tickets_per_day=_round2(tickets_per_day),
            trend=velocity_trend(this_week, last_week),
        ),
        specifications=spec_details,
        blockers=blockers,
        forecast=ImplementationForecast(
            all_specs_complete=(
                (moment + timedelta(days=overall_days_remaining)).isoformat()
                if overall_days_remaining is not None
                else None
            ),
            confidence=_confidence(tickets_per_day),
        ),
    )


def _safe_project_ref(stores: AllStores, project_id: str) -> ProjectRef:
    """Resolve a project ref, tolerating a missing project."""
    try:
        project = stores.projects.get_project(project_id)
    except NotFoundError:
        return ProjectRef("", "")
    return ProjectRef(project.id, project.name)


# =========================================================================== #
# time report (estimation-only)                                               #
# =========================================================================== #


def time_report(
    session_factory: sessionmaker[Session],
    *,
    now: Clock,
    project_id: str | None = None,
    specification_id: str | None = None,
    epic_id: str | None = None,
) -> TimeReport:
    """Estimation-only time report for an epic, spec, or project.

    ``actualMinutes`` is a frozen-zone WorkSession concern (absent in M0), so the
    report collapses to estimated-minutes sums (``by_epic`` / ``by_ticket`` /
    ``summary``). ``now`` is accepted for interface uniformity; the report is
    time-independent.
    """
    del now  # estimation-only report — no clock dependency.
    if not epic_id and not specification_id and not project_id:
        raise ValidationFailedError(
            "One of epicId, specificationId, or projectId is required."
        )

    with session_factory() as session:
        stores = make_stores(session)
        scope, epic_rows, ticket_rows = _resolve_time_scope(
            stores, project_id=project_id, specification_id=specification_id, epic_id=epic_id
        )

    by_ticket = [
        TimeByTicket(
            id=t.id,
            title=t.title,
            epic_id=t.epic_id,
            status=t.status.value,
            estimated_minutes=_est(t.estimated_minutes),
        )
        for t in ticket_rows
    ]
    by_epic = [
        TimeByEpic(
            id=epic_id_,
            title=title,
            specification_id=spec_id_,
            estimated_minutes=sum(bt.estimated_minutes for bt in by_ticket if bt.epic_id == epic_id_),
            ticket_count=sum(1 for bt in by_ticket if bt.epic_id == epic_id_),
        )
        for epic_id_, title, spec_id_ in epic_rows
    ]
    return TimeReport(
        scope=scope,
        summary=TimeSummary(total_estimated_minutes=sum(bt.estimated_minutes for bt in by_ticket)),
        by_epic=by_epic,
        by_ticket=by_ticket,
    )


def _resolve_time_scope(
    stores: AllStores,
    *,
    project_id: str | None,
    specification_id: str | None,
    epic_id: str | None,
) -> tuple[TimeScope, list[tuple[str, str, str]], list[TicketRecord]]:
    """Resolve (scope, [(epicId, epicTitle, specId)], tickets) for the time report."""
    epics: list[tuple[str, str, str]] = []
    tickets: list[TicketRecord] = []
    if epic_id:
        epic = stores.epics.get_epic(epic_id)
        epics.append((epic.id, epic.title, epic.specification_id))
        tickets = stores.tickets.list_tickets(epic_id=epic_id)
        return TimeScope("epic", epic_id, epic.title), epics, tickets
    if specification_id:
        spec = stores.specifications.get_specification(specification_id)
        for epic in stores.epics.list_epics(specification_id=specification_id):
            epics.append((epic.id, epic.title, epic.specification_id))
            tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
        return TimeScope("specification", specification_id, spec.title), epics, tickets
    assert project_id is not None  # mypy narrowing
    project = stores.projects.get_project(project_id)
    for spec in stores.specifications.list_specifications(project_id=project_id):
        for epic in stores.epics.list_epics(specification_id=spec.id):
            epics.append((epic.id, epic.title, epic.specification_id))
            tickets.extend(stores.tickets.list_tickets(epic_id=epic.id))
    return TimeScope("project", project_id, project.name), epics, tickets


# =========================================================================== #
# blockers report                                                             #
# =========================================================================== #


def blockers_report(
    session_factory: sessionmaker[Session],
    *,
    now: Clock,
    project_id: str | None = None,
    specification_id: str | None = None,
) -> BlockersReport:
    """The blocked tickets + their blocking dependencies (project- or spec-scoped).

    Each blocked ticket carries its immediate non-done dependencies (the blocker
    depth) and an injected-clock block duration.
    """
    if not project_id and not specification_id:
        raise ValidationFailedError("Either projectId or specificationId is required.")
    moment = _resolve_now(now)

    with session_factory() as session:
        stores = make_stores(session)
        if specification_id:
            spec = stores.specifications.get_specification(specification_id)
            scope = BlockerScope("specification", specification_id, spec.title)
            spec_refs = [EntityRef(spec.id, spec.title)]
        else:
            assert project_id is not None  # mypy narrowing
            project = stores.projects.get_project(project_id)
            scope = BlockerScope("project", project_id, project.name)
            spec_refs = [
                EntityRef(s.id, s.title)
                for s in stores.specifications.list_specifications(project_id=project_id)
            ]

        entries: list[BlockerEntry] = []
        by_spec: list[BySpecificationBlock] = []
        by_epic: list[ByEpicBlock] = []
        for spec_ref in spec_refs:
            epics = stores.epics.list_epics(specification_id=spec_ref.id)
            epic_by_id = {e.id: e for e in epics}
            spec_tickets = _spec_tickets(stores, spec_ref.id)
            ticket_by_id = {t.id: t for t in spec_tickets}
            deps_by = _deps_by_ticket(
                stores.ticket_dependencies.list_dependencies(specification_id=spec_ref.id)
            )

            spec_blocked = 0
            spec_minutes = 0
            epic_tally: dict[str, list[int]] = {}
            for ticket in (t for t in spec_tickets if t.block_reason is not None):
                duration = _duration_minutes(ticket.updated_at, moment)
                blocking = [
                    BlockerDependency(dep.id, dep.title, dep.status.value)
                    for dep in (ticket_by_id.get(d) for d in deps_by.get(ticket.id, []))
                    if dep is not None and dep.status != TicketStatus.DONE
                ]
                epic = epic_by_id.get(ticket.epic_id)
                epic_ref = EntityRef(ticket.epic_id, epic.title if epic else "")
                entries.append(
                    BlockerEntry(
                        ticket=BlockerTicket(
                            id=ticket.id,
                            title=ticket.title,
                            ticket_number=ticket.ticket_number or 0,
                            estimated_minutes=_est(ticket.estimated_minutes),
                        ),
                        epic=epic_ref,
                        specification=spec_ref,
                        block_info=BlockInfo(
                            reason=ticket.block_reason or "No reason provided",
                            updated_at=ticket.updated_at or moment.isoformat(),
                            duration_minutes=duration,
                            duration_formatted=_format_duration(duration),
                        ),
                        dependencies=blocking,
                    )
                )
                spec_blocked += 1
                spec_minutes += duration
                tally = epic_tally.setdefault(ticket.epic_id, [0, 0])
                tally[0] += 1
                tally[1] += duration

            by_spec.append(
                BySpecificationBlock(
                    id=spec_ref.id,
                    title=spec_ref.title,
                    blocked_count=spec_blocked,
                    total_blocked_minutes=spec_minutes,
                )
            )
            for epic_id_, (count, minutes) in epic_tally.items():
                epic = epic_by_id.get(epic_id_)
                by_epic.append(
                    ByEpicBlock(
                        id=epic_id_,
                        title=epic.title if epic else "",
                        specification_id=spec_ref.id,
                        blocked_count=count,
                        total_blocked_minutes=minutes,
                    )
                )

    by_duration = sorted(entries, key=lambda e: e.block_info.duration_minutes, reverse=True)
    total_minutes = sum(e.block_info.duration_minutes for e in entries)
    return BlockersReport(
        scope=scope,
        summary=BlockersSummary(
            total_blocked=len(entries),
            total_blocked_minutes=total_minutes,
            average_block_duration=(
                _round_half_up(total_minutes / len(entries)) if entries else 0
            ),
            oldest_blocker=by_duration[0].block_info.updated_at if by_duration else None,
        ),
        blockers=entries,
        by_specification=by_spec,
        by_epic=by_epic,
        longest_blocked=[
            LongestBlocked(
                ticket_id=e.ticket.id,
                title=e.ticket.title,
                duration_minutes=e.block_info.duration_minutes,
                reason=e.block_info.reason,
            )
            for e in by_duration[:5]
        ],
    )


# =========================================================================== #
# estimate drift (the >20% threshold)                                         #
# =========================================================================== #


def estimate_drift(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    now: Clock,
) -> list[EstimateDriftEntry]:
    """Flag epics whose stored estimate diverges from the live ticket-sum by ``>20%``.

    ``mismatchPercent = round(|epicMinutes - ticketSum| / epicMinutes * 100, 2)``;
    only epics with a positive stored estimate are considered, and only divergences
    strictly greater than ``20`` are returned. ``now`` is accepted
    for interface uniformity; the computation is time-independent.
    """
    del now  # threshold comparison — no clock dependency.
    if not specification_id:
        raise ValidationFailedError("specificationId is required for an estimate-drift report.")
    with session_factory() as session:
        stores = make_stores(session)
        stores.specifications.get_specification(specification_id)  # existence → NotFoundError
        return _compute_estimate_drift(stores, specification_id)


def _compute_estimate_drift(stores: AllStores, spec_id: str) -> list[EstimateDriftEntry]:
    """Pure drift computation over a spec's epics (shared by readiness)."""
    out: list[EstimateDriftEntry] = []
    for epic in stores.epics.list_epics(specification_id=spec_id):
        epic_minutes = epic.estimated_minutes
        if epic_minutes is None or epic_minutes <= 0:
            continue
        ticket_sum = sum(
            _est(t.estimated_minutes) for t in stores.tickets.list_tickets(epic_id=epic.id)
        )
        mismatch = _round2(abs(epic_minutes - ticket_sum) / epic_minutes * 100)
        if mismatch > 20:
            out.append(
                EstimateDriftEntry(
                    epic_id=epic.id,
                    epic_title=epic.title,
                    epic_estimated_minutes=epic_minutes,
                    ticket_sum_minutes=ticket_sum,
                    mismatch_percent=mismatch,
                )
            )
    return out


# =========================================================================== #
# readiness (degraded — NO-LLM)                                               #
# =========================================================================== #


def readiness_report(
    session_factory: sessionmaker[Session],
    specification_id: str,
    *,
    now: Clock,
) -> ReadinessReport:
    """Readiness report that degrades gracefully under NO-LLM.

    The AI readiness score fields do not exist in SpecSmither, so spec/epic/ticket
    scores surface as ``None`` and the score-driven ``attention_items`` are empty.
    The deterministic categories — file conflicts, dependency health and the
    estimate drift — are computed for real. ``now`` is accepted for interface
    uniformity; the report is time-independent.
    """
    del now  # readiness is structural — no clock dependency.
    if not specification_id:
        raise ValidationFailedError("specificationId is required for a readiness report.")

    with session_factory() as session:
        stores = make_stores(session)
        stores.specifications.get_specification(specification_id)  # existence → NotFoundError
        epics = stores.epics.list_epics(specification_id=specification_id)

        all_tickets: list[TicketRecord] = []
        epic_breakdown: list[EpicReadinessBreakdown] = []
        for epic in epics:
            tickets = stores.tickets.list_tickets(epic_id=epic.id)
            all_tickets.extend(tickets)
            epic_breakdown.append(
                EpicReadinessBreakdown(
                    epic_id=epic.id,
                    epic_title=epic.title,
                    avg_score=None,
                    global_readiness_score=None,
                    global_readiness_level=None,
                    ticket_count=len(tickets),
                    ticket_scores=[TicketScore(t.id, t.title, None, None) for t in tickets],
                    attention_items=[],
                )
            )

        edges = stores.ticket_dependencies.list_dependencies(specification_id=specification_id)
        estimate_health = _compute_estimate_drift(stores, specification_id)

    # --- file conflicts ---
    file_to_tickets: dict[str, list[EntityRef]] = {}
    for ticket in all_tickets:
        for path in ticket.files_to_be_modified:
            file_to_tickets.setdefault(path, []).append(EntityRef(ticket.id, ticket.title))

    pairs: set[tuple[str, str]] = set()
    for edge in edges:
        pairs.add((edge.ticket_id, edge.depends_on_id))
        pairs.add((edge.depends_on_id, edge.ticket_id))

    file_conflicts: list[FileConflict] = []
    for path, refs in file_to_tickets.items():
        if len(refs) <= 1:
            continue
        has_dependency = any(
            (refs[i].id, refs[j].id) in pairs
            for i in range(len(refs))
            for j in range(i + 1, len(refs))
        )
        file_conflicts.append(FileConflict(path, refs, has_dependency))

    # --- dependency health ---
    ids_with_deps = {tid for pair in pairs for tid in pair}
    orphans = [EntityRef(t.id, t.title) for t in all_tickets if t.id not in ids_with_deps]
    dependency_health = DependencyHealth(
        total_dependencies=len(pairs) // 2,
        orphan_tickets=orphans,
    )

    # --- attention points (no low_readiness — scores are absent) ---
    attention: list[AttentionPoint] = []
    for conflict in file_conflicts:
        if not conflict.has_dependency:
            attention.append(
                AttentionPoint(
                    severity="warning",
                    category="file_conflict",
                    message=(
                        f'File "{conflict.file_path}" modified by '
                        f"{len(conflict.tickets)} tickets without dependency"
                    ),
                    entity_id=conflict.file_path,
                    entity_title=", ".join(t.title for t in conflict.tickets),
                )
            )
    for mismatch in estimate_health:
        attention.append(
            AttentionPoint(
                severity="warning",
                category="estimate_mismatch",
                message=(
                    f"Epic estimate ({mismatch.epic_estimated_minutes}m) differs from "
                    f"ticket sum ({mismatch.ticket_sum_minutes}m) by {mismatch.mismatch_percent}%"
                ),
                entity_id=mismatch.epic_id,
                entity_title=mismatch.epic_title,
            )
        )
    attention.sort(key=lambda a: 0 if a.severity == "error" else 1)

    return ReadinessReport(
        spec_readiness=SpecReadiness(specification_id, None, None, None, None),
        epic_breakdown=epic_breakdown,
        file_conflicts=file_conflicts,
        dependency_health=dependency_health,
        estimate_health=estimate_health,
        attention_points=attention,
    )


# =========================================================================== #
# active sessions (planning only)                                             #
# =========================================================================== #


def active_sessions(
    session_factory: sessionmaker[Session],
    project_id: str,
    *,
    now: Clock,
) -> ActiveSessions:
    """Active planning sessions for a project (work/review are frozen-zone — empty).

    ``now`` is accepted for interface uniformity; the dashboard is a point-in-time
    snapshot with no clock arithmetic.
    """
    del now  # snapshot — no clock dependency.
    if not project_id:
        raise ValidationFailedError("projectId is required for an active-sessions report.")

    with session_factory() as session:
        stores = make_stores(session)
        stores.projects.get_project(project_id)  # existence → NotFoundError
        specs = stores.specifications.list_specifications(project_id=project_id)
        title_by_spec = {s.id: s.title for s in specs}

        planning: list[ActiveSession] = []
        for spec in specs:
            for sess in stores.planning_sessions.list_planning_sessions(specification_id=spec.id):
                if len(planning) >= 50:
                    break
                if sess.status == PlanningSessionStatus.ACTIVE.value:
                    planning.append(
                        ActiveSession(
                            session_id=sess.id,
                            specification_id=sess.specification_id,
                            spec_title=title_by_spec.get(sess.specification_id),
                            current_phase=sess.current_phase,
                            started_at=sess.started_at,
                            actions_count=sess.actions_count or 0,
                        )
                    )

    return ActiveSessions(
        planning=planning,
        work=[],
        review=[],
        summary=SessionsSummary(
            total_active=len(planning),
            by_type=SessionsByType(planning=len(planning), work=0, review=0),
        ),
    )


# =========================================================================== #
# dropped report kinds (AI / frozen-zone)                                     #
# =========================================================================== #


def work_report(*, now: Clock | None = None) -> NoReturn:
    """The ``work`` report is unavailable in SpecSmither M0.

    It depends on the frozen-zone work lifecycle (WorkSession aggregation), which
    does not exist here. ``now`` is accepted only to mirror the report signature.
    """
    raise NotImplementedError(
        "Report type 'work' is not available in SpecSmither M0 — it depends on the "
        "frozen-zone work lifecycle (no work sessions)."
    )


def implementation_analysis_report(*, now: Clock | None = None) -> NoReturn:
    """The ``implementation_analysis`` report is unavailable in SpecSmither M0.

    It depends on the frozen-zone record-events store. ``now`` is accepted only to
    mirror the report signature.
    """
    raise NotImplementedError(
        "Report type 'implementation_analysis' is not available in SpecSmither M0 — "
        "it depends on the frozen-zone record-events store."
    )
