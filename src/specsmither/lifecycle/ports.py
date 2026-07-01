"""The lifecycle seam — ports (Protocols) + the data contracts they exchange.

Faithful port of ``lifecycle/ports.ts`` + ``lifecycle/types.ts`` (A1 §3.1, §5).
This module is the contract every verb, pre-check, gate, and adapter depends on;
it is **pure** — typing :class:`~typing.Protocol`\\ s and frozen dataclasses, with
**no concrete implementations**. The concrete SQLite stores / crucible validator /
in-memory projector / WritePlan executor (the COUPLED surface) live under
``specsmither.adapters`` and satisfy these Protocols structurally.

What the seam carries
---------------------

* :class:`ValidatorOutput` — *the* contract the crucible validator adapter must
  satisfy. The lifecycle only ever reads ``gate_result``, ``local_score``,
  ``per_epic_score[id]``, ``per_ticket_score[id]`` and ``findings`` (message +
  ``entity_id`` for hint grouping); it never inspects scoring internals (A1 §3.1).
* :class:`SpecFull` — the read contract the spec store produces. ``spec`` is the
  nested crucible :class:`~crucible.models.Specification` fed straight to the
  validator/gate (it is the single source of truth, exactly what
  :func:`specsmither.domain.records.build_spec_full` recomposes). The flat
  ``epics`` / ``blueprints`` / ``dependencies`` side-projections power the
  cascade-rules / cross-cut / batch-cycle pre-checks (A1 §5).

The ports (method names mirror the TS, snake_cased; **sync** — the ``Promise``
wrappers drop because the engine is fully deterministic, no async I/O):

* :class:`PlanningSessionStore` — read planning-session rows.
* :class:`SpecStore` — read spec state (flat + recomposed ``SpecFull``).
* :class:`ConfigStore` — project overrides / spec snapshots (config-store seam).
* :class:`Validator` — score a :class:`SpecFull` for a phase → :class:`ValidatorOutput`.
* :class:`OperationsLayer` — project a logical mutation onto a ``SpecFull`` in
  memory (no persist), so the gate can score *as-if-applied*.

:class:`LifecyclePorts` bundles all of the above plus the injected
``persist_write_plan`` / ``clock`` / ``id_generator`` — the bag
``create_lifecycle(ports)`` takes. ``persist_write_plan`` / ``clock`` /
``id_generator`` are optional (the in-memory/dev path may omit ``persist``; when
omitted the engine builds the plan but does not commit). The ULID id generator is
required in production — the audit projector treats ``id`` as a chronological
cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from crucible.models import Specification

from specsmither.adapters.write_plan_executor import WritePlan
from specsmither.db.models import PlanningSession
from specsmither.domain.enums import FindingCategory, PlanningPhase

__all__ = [
    "BlueprintRef",
    "Clock",
    "ConfigStore",
    "EpicFull",
    "IdGenerator",
    "LifecyclePorts",
    "OperationsLayer",
    "PersistWritePlan",
    "PlanningSessionStore",
    "ProjectConfigEntry",
    "SpecConfigEntry",
    "SpecDependencyEdge",
    "SpecFull",
    "SpecStore",
    "TicketRef",
    "Validator",
    "ValidatorFinding",
    "ValidatorOutput",
]


# --------------------------------------------------------------------------- #
# Injected-callable port aliases                                              #
# --------------------------------------------------------------------------- #

#: The now-provider (``ports.clock``). Injected for determinism; mirrors the TS
#: ``clock?: () => Date``.
Clock = Callable[[], datetime]

#: The id minter (``ports.idGenerator``). Must produce a monotonic **ULID** — the
#: audit projector orders rows by ``id`` as a chronological cursor.
IdGenerator = Callable[[], str]

#: Commit a verb's :class:`WritePlan`. The lifecycle *builds* the plan; this port
#: *persists* it (one SQLite transaction via the executor adapter). Optional: the
#: in-memory/dev path omits it and the engine then does not persist.
PersistWritePlan = Callable[[WritePlan], None]


# --------------------------------------------------------------------------- #
# Validator output contract (types.ts:8-26)                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ValidatorFinding:
    """A single validator finding (``ValidatorFinding``, types.ts:17-26).

    ``severity`` distinguishes a soft ``finding`` (points lost, advisory) from a
    hard ``denial`` (gate-blocking). ``points_lost`` / ``global_impact_on_fix``
    are advisory deltas surfaced in guidance prose. Required fields are listed
    first (``category`` / ``message`` / ``severity``); the optional locators
    default to ``None`` (Python dataclass ordering — the TS field *names* are
    preserved, only the declaration order is adapted).
    """

    category: FindingCategory | str
    message: str
    severity: Literal["finding", "denial"]
    entity_id: str | None = None
    entity_type: str | None = None
    path: str | None = None
    points_lost: float | None = None
    global_impact_on_fix: float | None = None


@dataclass(frozen=True)
class ValidatorOutput:
    """The validator output the lifecycle consumes (``ValidatorOutput``, types.ts:8-15).

    **This is the entire contract the crucible validator adapter must satisfy.**

    * ``gate_result`` — ``'pass'`` / ``'fail'`` (the validator already folds
      score-vs-threshold AND cascade for the binary phases).
    * ``local_score`` — spec-level score (0..1).
    * ``per_epic_score`` / ``per_ticket_score`` — populated only on the matching
      ``*_expansion`` phase; ``{}`` otherwise.
    * ``findings`` — flat finding list (rubric / count / cross-cut / cascade /
      cross-validation / schema).
    * ``validated_phase`` — the phase actually validated (the late-op rollback
      ``effectivePhase``); the SPS-resume cache is keyed on this.
    """

    gate_result: Literal["pass", "fail"]
    local_score: float
    per_epic_score: dict[str, float]
    per_ticket_score: dict[str, float]
    findings: list[ValidatorFinding]
    validated_phase: PlanningPhase


# --------------------------------------------------------------------------- #
# SpecFull read contract (ports.ts:31-71)                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpecDependencyEdge:
    """A ticket-level dependency edge (``SpecDependencyEdge``, ports.ts:31-34).

    ``from_ticket_id`` depends on ``to_ticket_id``. The shape matches the planning
    batch-validation edge so the action verb can pass ``spec_full.dependencies``
    straight into the batch cycle pre-check. Powers cascade-rules, cross-cut, and
    the cross-existing cycle/dedup check.
    """

    from_ticket_id: str
    to_ticket_id: str


@dataclass(frozen=True)
class TicketRef:
    """A flat ticket projection inside :class:`EpicFull` (``TicketRef``, ports.ts:57-63).

    ``extra`` captures any additional ticket columns (the TS ``[key]: unknown``
    index signature) the pre-checks may read without widening the typed surface.
    """

    id: str
    epic_id: str
    title: str
    description: str | None = None
    status: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EpicFull:
    """A flat epic projection with its tickets (``EpicFull``, ports.ts:48-55).

    ``extra`` captures the TS ``[key]: unknown`` index-signature columns (e.g. the
    epic JSON the cross-cut referrer scan stringifies).
    """

    id: str
    specification_id: str
    title: str
    tickets: list[TicketRef]
    description: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BlueprintRef:
    """A flat blueprint projection (``BlueprintRef``, ports.ts:65-71).

    ``extra`` captures the TS ``[key]: unknown`` index-signature columns.
    """

    id: str
    specification_id: str
    title: str
    category: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpecFull:
    """Full spec with all related entities (``SpecFull``, ports.ts:38-46).

    ``spec`` is the nested crucible :class:`~crucible.models.Specification` — the
    single source of truth the validator/gate scores directly. ``epics`` /
    ``blueprints`` are flat side-projections; ``dependencies`` is the ticket-level
    dependency graph (empty = no edges) that powers persisted-overlap dedup +
    cross-existing cycle detection in the action-verb pre-checks.
    """

    spec: Specification
    epics: list[EpicFull]
    blueprints: list[BlueprintRef]
    dependencies: list[SpecDependencyEdge] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# ConfigStore list-entry value shapes (config-store-interface.ts:4,8)         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProjectConfigEntry:
    """One row of :meth:`ConfigStore.list_project_configs`.

    ``{domain, overrides, schemaVersion}`` (config-store-interface.ts:4).
    """

    domain: str
    overrides: Any
    schema_version: int


@dataclass(frozen=True)
class SpecConfigEntry:
    """One row of :meth:`ConfigStore.list_spec_configs`.

    ``{domain, snapshot, schemaVersion}`` (config-store-interface.ts:8).
    """

    domain: str
    snapshot: Any
    schema_version: int


# --------------------------------------------------------------------------- #
# Ports (Protocols)                                                           #
# --------------------------------------------------------------------------- #


@runtime_checkable
class PlanningSessionStore(Protocol):
    """Read planning-session rows (``IPlanningSessionStore``, planning-session-store.ts).

    The production impl is a SQLite ``planning_sessions`` table guarded by the
    ``UNIQUE(specification_id) WHERE status != 'closed'`` partial index (the
    one-active-session lock). Pagination from the TS ``Paginated<T>`` collapses to
    plain lists locally.
    """

    def get_planning_session(self, session_id: str) -> PlanningSession | None: ...

    def get_active_planning_session_by_spec(self, spec_id: str) -> PlanningSession | None: ...

    def list_planning_sessions_by_spec(self, spec_id: str) -> list[PlanningSession]: ...

    def list_planning_sessions_by_project(self, project_id: str) -> list[PlanningSession]: ...


@runtime_checkable
class SpecStore(Protocol):
    """Read spec state (``ISpecStore``, ports.ts:81-84).

    ``get_spec`` returns the flat/nested crucible :class:`~crucible.models.Specification`;
    ``get_spec_full`` recomposes child tables into a :class:`SpecFull`. Both return
    ``None`` for an unknown spec.
    """

    def get_spec(self, spec_id: str) -> Specification | None: ...

    def get_spec_full(self, spec_id: str) -> SpecFull | None: ...


@runtime_checkable
class ConfigStore(Protocol):
    """Project overrides / spec snapshots (``IConfigStore``, config-store-interface.ts).

    The effective ``ValidatorConfig`` is assembled by
    ``crucible.merge_config(crucible.load_defaults(), overrides)`` where the
    overrides come from :meth:`get_project_overrides` (and any frozen
    :meth:`get_spec_snapshot`). ``domain`` selects the config namespace
    (``'planning'`` / ``'planning-lifecycle'``). The production impl is a SQLite
    ``config`` table.
    """

    def get_project_overrides(self, project_id: str, domain: str) -> Any | None: ...

    def set_project_overrides(
        self, project_id: str, domain: str, overrides: Any, schema_version: int
    ) -> None: ...

    def list_project_configs(self, project_id: str) -> list[ProjectConfigEntry]: ...

    def get_spec_snapshot(self, spec_id: str, domain: str) -> Any | None: ...

    def set_spec_snapshot(
        self, spec_id: str, domain: str, snapshot: Any, schema_version: int
    ) -> None: ...

    def list_spec_configs(self, spec_id: str) -> list[SpecConfigEntry]: ...


@runtime_checkable
class Validator(Protocol):
    """Score a :class:`SpecFull` for a phase (``IValidator``, ports.ts:74-76).

    The production impl wraps crucible's synchronous ``validate`` and maps its
    richer result down to :class:`ValidatorOutput`. ``config`` is the effective
    merged ``ValidatorConfig`` dict.
    """

    def validate(
        self, spec_full: SpecFull, phase: PlanningPhase, config: Mapping[str, Any]
    ) -> ValidatorOutput: ...


@runtime_checkable
class OperationsLayer(Protocol):
    """In-memory logical mutation projector (``IOperationsLayer``, ports.ts:87-95).

    :meth:`apply_mutation` projects ``op``/``payload`` onto a :class:`SpecFull`
    snapshot and returns the projected state. It does **not** persist — the
    lifecycle calls it to compute post-mutation state for the validator/gate.
    """

    def apply_mutation(
        self, op: str, payload: Mapping[str, Any], spec_full: SpecFull
    ) -> SpecFull: ...


# --------------------------------------------------------------------------- #
# The port bundle (LifecyclePorts, ports.ts:105-123)                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LifecyclePorts:
    """The injected seam bundle ``create_lifecycle(ports)`` takes (``LifecyclePorts``).

    The five stores/adapters are required; ``persist_write_plan`` / ``clock`` /
    ``id_generator`` are optional (mirroring the TS ``?`` fields). ``config_store``
    replaces the TS pair of async config resolvers (``planningConfig`` /
    ``planningLifecycleConfig``) — SpecSmither assembles the effective config from
    the store directly (the async ``PlanningConfigResolver`` is skipped). When
    ``persist_write_plan`` is omitted the engine builds the plan but does not
    commit; production MUST wire it.
    """

    planning_session_store: PlanningSessionStore
    spec_store: SpecStore
    config_store: ConfigStore
    validator: Validator
    operations: OperationsLayer
    persist_write_plan: PersistWritePlan | None = None
    clock: Clock | None = None
    id_generator: IdGenerator | None = None
