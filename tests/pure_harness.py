"""A DB-free harness for driving the lifecycle verbs as PURE functions.

This is the parity anchor for the ``specsmither-lifecycle`` extraction: it feeds
each verb its state **as in-memory data** (a real :class:`SpecFull` built with
:func:`build_spec_full`, a :class:`PlanningSessionRecord`, defaulted config) through
a bag of fake ports, calls the verb directly, and hands back the returned
:class:`WritePlan` — **no SQLite, no transaction, no persistence**. The verbs
already satisfy the pure contract ``(payload, ports) -> {response, WritePlan}``;
this harness exercises exactly that surface so the extraction can be proven
behaviour-preserving (the WritePlan a verb builds must not change).

Two construction sites deliberately live in ONE place so the extraction touches
only them:

* :func:`make_planning_session` builds the session STATE object — now the pure,
  DB-free ``PlanningSessionRecord`` (the ORM coupling the extraction removed); the
  concrete SQLite store maps its ORM row to the same record, and every test below
  keeps asserting the same WritePlans.
* :func:`pure_ports` wires the operations projector via the pure
  ``ProjectorOperationsLayer`` from :mod:`specsmither.lifecycle.operations_projector`
  (the DB-free projector — no sqlalchemy on the path).

Neither swap changes any asserted WritePlan — that is the whole point.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

from crucible.models import Specification
from crucible.models.enums import BlueprintCategory, Complexity, TicketType

from specsmither.domain.enums import PlanningPhase, PlanningSessionStatus, SpecStatus, TicketStatus
from specsmither.domain.records import (
    BlueprintRecord,
    DependencyEdge,
    EpicRecord,
    SpecificationRecord,
    TicketRecord,
    build_spec_full,
)
from specsmither.lifecycle.operations_projector import (
    ProjectorOperationsLayer,
    _spec_full_from_spec,
)
from specsmither.lifecycle.ports import LifecyclePorts, SpecFull, ValidatorFinding, ValidatorOutput
from specsmither.lifecycle.session_record import PlanningSessionRecord

__all__ = [
    "EPIC_ID",
    "FIXED_NOW",
    "PROJECT_ID",
    "SPEC_ID",
    "SeqIds",
    "StubValidator",
    "demo_spec_full",
    "fixed_clock",
    "make_planning_session",
    "plan_to_dict",
    "pure_ports",
]

SPEC_ID = "spec-0001"
PROJECT_ID = "proj-0001"
EPIC_ID = "epic-0001"
FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def fixed_clock() -> datetime:
    """A frozen wall clock so timestamps in the WritePlan are deterministic."""
    return FIXED_NOW


class SeqIds:
    """A deterministic id generator: ``gen-0001``, ``gen-0002``, … (per instance)."""

    def __init__(self, prefix: str = "gen") -> None:
        self.prefix = prefix
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}-{self.n:04d}"


class StubValidator:
    """A configurable validator — drives the gate deterministically, no crucible run."""

    def __init__(
        self,
        *,
        gate_result: str = "pass",
        local_score: float = 100.0,
        per_epic_score: dict[str, float] | None = None,
        per_ticket_score: dict[str, float] | None = None,
        findings: list[ValidatorFinding] | None = None,
    ) -> None:
        self.gate_result = gate_result
        self.local_score = local_score
        self.per_epic_score = per_epic_score or {}
        self.per_ticket_score = per_ticket_score or {}
        self.findings = findings or []
        self.calls: list[PlanningPhase] = []
        self.languages: list[str] = []

    def validate(
        self,
        spec_full: SpecFull,
        phase: PlanningPhase,
        config: Mapping[str, Any],
        language: str = "en",
    ) -> ValidatorOutput:
        self.calls.append(phase)
        self.languages.append(language)
        return ValidatorOutput(
            gate_result=self.gate_result,  # type: ignore[arg-type]
            local_score=self.local_score,
            per_epic_score=dict(self.per_epic_score),
            per_ticket_score=dict(self.per_ticket_score),
            findings=list(self.findings),
            validated_phase=phase,
        )


class _FakeSpecStore:
    def __init__(self, spec_full: SpecFull | None) -> None:
        self._spec_full = spec_full

    def get_spec(self, spec_id: str) -> Specification | None:
        return self._spec_full.spec if self._spec_full is not None else None

    def get_spec_full(self, spec_id: str) -> SpecFull | None:
        return self._spec_full


class _FakeSessionStore:
    def __init__(self, session: PlanningSessionRecord | None) -> None:
        self._session = session

    def get_planning_session(self, session_id: str) -> PlanningSessionRecord | None:
        return self._session

    def get_active_planning_session_by_spec(self, spec_id: str) -> PlanningSessionRecord | None:
        return self._session

    def list_planning_sessions_by_spec(self, spec_id: str) -> list[PlanningSessionRecord]:
        return [self._session] if self._session is not None else []

    def list_planning_sessions_by_project(self, project_id: str) -> list[PlanningSessionRecord]:
        return [self._session] if self._session is not None else []


class _DefaultConfigStore:
    """Returns nothing → ``resolve_*_config`` falls back to crucible defaults."""

    def get_project_overrides(self, project_id: str, domain: str) -> Any | None:
        return None

    def set_project_overrides(
        self, project_id: str, domain: str, overrides: Any, schema_version: int
    ) -> None:
        return None

    def list_project_configs(self, project_id: str) -> list[Any]:
        return []

    def get_spec_snapshot(self, spec_id: str, domain: str) -> Any | None:
        return None

    def set_spec_snapshot(
        self, spec_id: str, domain: str, snapshot: Any, schema_version: int
    ) -> None:
        return None

    def list_spec_configs(self, spec_id: str) -> list[Any]:
        return []


def make_planning_session(**overrides: Any) -> PlanningSessionRecord:
    """Build the session STATE object from plain fields (the ONE construction site).

    Returns the pure, DB-free :class:`PlanningSessionRecord` the verbs now read; the
    concrete SQLite store maps its ORM row to the same record. Sets every attribute the
    verbs read to a concrete value.
    """
    fields: dict[str, Any] = {
        "id": "sess-0001",
        "specification_id": SPEC_ID,
        "status": PlanningSessionStatus.ACTIVE.value,
        "current_phase": PlanningPhase.PLANNING_SPEC.value,
        "actions_count": 0,
        "last_score": None,
        "last_gate_result": None,
        "pending_human_feedback": None,
        "last_transition_trigger": None,
        "last_transition_at": None,
        "last_read_at": None,
        "last_validated_at": None,
        "last_validator_output": None,
        "started_at": FIXED_NOW.isoformat(),
        "last_action_at": FIXED_NOW.isoformat(),
    }
    fields.update(overrides)
    return PlanningSessionRecord(**fields)


def demo_spec_full(*, status: SpecStatus = SpecStatus.PLANNING) -> SpecFull:
    """A small but real :class:`SpecFull` (1 epic, 2 tickets, 1 blueprint) built purely."""
    spec = SpecificationRecord(
        id=SPEC_ID, project_id=PROJECT_ID, title="Harness spec", status=status
    )
    epic = EpicRecord(
        id=EPIC_ID,
        specification_id=SPEC_ID,
        title="Foundation",
        description="Lay the foundation.",
        objective="Establish the base.",
        epic_number=1,
        order=0,
    )
    ticket_a = TicketRecord(
        id="ticket-a",
        epic_id=EPIC_ID,
        title="First ticket",
        ticket_number=1,
        order=0,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.SMALL,
        estimated_minutes=30,
        status=TicketStatus.PENDING,
    )
    ticket_b = TicketRecord(
        id="ticket-b",
        epic_id=EPIC_ID,
        title="Second ticket",
        ticket_number=2,
        order=1,
        ticket_type=TicketType.IMPLEMENTATION,
        complexity=Complexity.MEDIUM,
        estimated_minutes=60,
        status=TicketStatus.PENDING,
    )
    blueprint = BlueprintRecord(
        id="bp-0001",
        specification_id=SPEC_ID,
        title="System diagram",
        category=BlueprintCategory.ARCHITECTURE,
        content="graph TD; A-->B",
        order=0,
    )
    deps: list[DependencyEdge] = []
    # ``build_spec_full`` yields the nested crucible Specification (the projector/gate
    # source of truth); ``_spec_full_from_spec`` re-derives the flat ports view the
    # SpecStore hands back. Both are pure (no DB).
    nested = build_spec_full(spec, [epic], [ticket_a, ticket_b], [blueprint], deps)
    return _spec_full_from_spec(nested)


def pure_ports(
    *,
    spec_full: SpecFull | None = None,
    session: PlanningSessionRecord | None = None,
    validator: StubValidator | None = None,
    id_generator: SeqIds | None = None,
) -> LifecyclePorts:
    """Assemble a fully in-memory :class:`LifecyclePorts` — no DB, no persist.

    ``persist_write_plan`` is intentionally ``None`` (callers read the returned
    ``VerbResult.write_plan`` directly). ``operations`` is the REAL projector so the
    post-mutation state fed to the gate is genuine.
    """
    return LifecyclePorts(
        planning_session_store=_FakeSessionStore(session),
        spec_store=_FakeSpecStore(spec_full),
        config_store=_DefaultConfigStore(),
        validator=validator or StubValidator(),
        operations=ProjectorOperationsLayer(),
        persist_write_plan=None,
        clock=fixed_clock,
        id_generator=id_generator or SeqIds(),
    )


def _normalize(value: Any) -> Any:
    """Recursively coerce a dataclass-derived structure to JSON-comparable scalars."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    return value


def plan_to_dict(write_plan: Any) -> dict[str, Any]:
    """Serialize a :class:`WritePlan` to a normalized, JSON-comparable dict.

    Proves the WritePlan is a portable data contract (the seam any executor — the
    local SQLite one or an external Postgres one — consumes) AND gives a stable
    golden to assert behaviour parity across the extraction.
    """
    return cast("dict[str, Any]", _normalize(dataclasses.asdict(write_plan)))
