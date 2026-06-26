"""Runtime DB-row DTOs + the ``SpecFull`` recompose.

These are the *flat* persistence-row shapes (a clean port of
``api-types/runtime/{specification,epic,ticket,blueprint}-record.ts``) — the
denormalized columns + JSON fields + count rollups the SQLite repositories read
and write. They are deliberately distinct from crucible's *nested* authoring
models (``crucible.models.{Specification,Epic,Ticket,Blueprint}``): a record is
one row, the crucible model is the whole tree.

Two halves:

1. **Records** — pure pydantic DTOs (no SQLAlchemy), JSON-serializable, following
   crucible's :class:`~crucible.models._base.Entity` convention (snake_case attrs
   with auto camelCase aliases, ``populate_by_name``, ``extra="allow"``) so they
   round-trip to the same camelCase wire shape crucible expects. Cloud-only fields
   are dropped (``contentS3Key``, the ``projectId`` denorm on child rows, S3 / GSI
   / auth / review-session fields). Where the SpecSmither schema (architecture §3)
   diverges from the TS record we follow §3 (e.g. the ``last_validator_output`` /
   ``dependency_tree`` / graph-metric columns; the BDD ``given``/``when``/``then``
   acceptance-criterion triple that replaced the legacy flat ``description`` in
   M1.5.4; ``coverage_type`` on blueprints).

2. :func:`build_spec_full` — assembles the flat records back into crucible's nested
   :class:`crucible.models.Specification` (epics carrying ordered tickets; tickets
   carrying acceptance criteria / implementation steps / file lists / dependency
   links; spec carrying blueprints). This is the ``SpecFull`` builder the planning
   gate (the crucible validator adapter) scores.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from crucible.models import (
    AcceptanceCriterion,
    ApiContract,
    Blueprint,
    BlueprintReference,
    CodeReference,
    DependencyLink,
    Epic,
    EpicTargets,
    FieldDeclaration,
    Goal,
    Guardrail,
    ImplementationStep,
    NonFunctionalRequirement,
    Requirement,
    Scope,
    SharedPattern,
    Specification,
    StructureItem,
    TechStackItem,
    TestSpecification,
    Ticket,
    TypeReference,
)
from crucible.models.enums import (
    BlueprintCategory,
    BlueprintCoverageType,
    BlueprintFormat,
    Complexity,
    DependencyType,
    EpicCategory,
    TestType,
    TicketType,
)
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from specsmither.domain.enums import EpicStatus, SpecStatus, TicketStatus

__all__ = [
    "BlueprintRecord",
    "DependencyEdge",
    "EpicRecord",
    "SpecificationRecord",
    "TicketBlueprintRef",
    "TicketRecord",
    "build_spec_full",
]


class _RecordBase(BaseModel):
    """Shared config for the runtime row DTOs (mirrors crucible's ``Entity``)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


# ---------------------------------------------------------------------------
# Edge / join rows
# ---------------------------------------------------------------------------


class DependencyEdge(_RecordBase):
    """A ``ticket_dependencies`` row (a directed edge ``ticket → depends_on``).

    ``ticket_id`` is the dependent ticket; ``depends_on_id`` is the ticket it
    depends on. Mirrors ``TicketDependency`` (``ticket-record.ts``); the unique
    key is ``(ticket_id, depends_on_id)``.
    """

    id: str | None = None
    ticket_id: str
    depends_on_id: str
    type: DependencyType
    created_at: str | None = None


class TicketBlueprintRef(_RecordBase):
    """A ``ticket_blueprint_refs`` join row (``TicketBlueprintReference``)."""

    id: str | None = None
    ticket_id: str
    blueprint_id: str
    context: str | None = None
    section: str | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Entity rows
# ---------------------------------------------------------------------------


class BlueprintRecord(_RecordBase):
    """A ``blueprints`` row (port of ``BlueprintRecord`` + architecture §3).

    Drops ``contentS3Key`` (content is stored inline); adds ``coverage_type``
    (§3). ``status`` is a DB-only publication state with no crucible counterpart.
    """

    id: str
    specification_id: str
    title: str
    category: BlueprintCategory
    description: str | None = None
    slug: str | None = None
    format: BlueprintFormat | None = None
    coverage_type: BlueprintCoverageType = BlueprintCoverageType.TICKET
    content: str = ""
    notes: str | None = None
    version: str | None = None
    order: int | None = None
    status: str | None = None
    tags: list[str] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class TicketRecord(_RecordBase):
    """A fully-hydrated ``tickets`` row (port of ``TicketFull`` + child recompose).

    Runtime-state fields (``status``/``block_reason``/``progress``/
    ``current_work_session_id``) stay on the ticket (ME.0 §12.3). The flat test
    fields (``quality_gates``/``test_commands``/``coverage_target``) live on the
    row; only ``test_types`` is normalized into a child table — here it rides back
    as the recomposed list. Acceptance criteria, implementation steps and the four
    ``files_to_be_*`` lists are the child-table arrays hydrated onto the row; they
    reuse crucible's sub-models so :func:`build_spec_full` passes them straight
    through. Cloud-only ``projectId`` denorm (on the file-change / test child rows)
    is dropped.
    """

    id: str
    epic_id: str
    title: str
    ticket_number: int | None = None
    description: str | None = None
    status: TicketStatus = TicketStatus.PENDING
    ticket_type: TicketType | None = None
    complexity: Complexity | None = None
    estimated_minutes: int | None = None
    planning_type: str | None = None
    block_reason: str | None = None
    progress: int = 0
    order: int | None = None
    current_work_session_id: str | None = None
    tags: list[str] | None = None

    # Denormalized count columns (aggregator-owned).
    incoming_dep_count: int = 0
    outgoing_dep_count: int = 0
    blueprint_count: int = 0

    # Flat test fields (only `test_types` is normalized out into a child table).
    test_types: list[TestType] = Field(default_factory=list)
    quality_gates: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    coverage_target: float | None = None

    # JSON content columns.
    code_references: list[CodeReference] = Field(default_factory=list)
    type_references: list[TypeReference] = Field(default_factory=list)
    field_declarations: dict[str, FieldDeclaration] | None = None

    # Child-backed arrays (recomposed-on-read).
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    implementation_steps: list[ImplementationStep] = Field(default_factory=list)
    files_to_be_created: list[str] = Field(default_factory=list)
    files_to_be_modified: list[str] = Field(default_factory=list)
    files_to_be_deleted: list[str] = Field(default_factory=list)
    files_to_be_referenced: list[str] = Field(default_factory=list)

    created_at: str | None = None
    updated_at: str | None = None


class EpicRecord(_RecordBase):
    """An ``epics`` row (port of ``EpicFull``).

    Spec/epic content arrays are stored as JSON columns (not child tables) and
    reuse crucible's structured sub-models. AI-readiness columns are dropped
    (SpecSmither is NO-LLM). ``planning_type`` is kept as a loose string (the
    ``review`` value is unreachable — no review lifecycle).
    """

    id: str
    specification_id: str
    title: str
    description: str
    objective: str
    epic_number: int | None = None
    order: int | None = None
    status: EpicStatus = EpicStatus.TODO
    planning_type: str | None = None
    progress: int = 0
    category: EpicCategory | None = None

    architecture: str | None = None
    scope: Scope | None = None
    goals: list[Goal] | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    validation_commands: list[str] | None = None
    api_contracts: list[ApiContract] | None = None
    shared_patterns: list[SharedPattern] | None = None
    file_structures: list[StructureItem] | None = None
    requirements_covered: list[str] | None = None
    nfrs_covered: list[str] | None = None
    goals_covered: list[str] | None = None
    field_declarations: dict[str, FieldDeclaration] | None = None

    # Denormalized count columns (aggregator-owned).
    ticket_count: int = 0
    completed_ticket_count: int = 0
    pending_ticket_count: int = 0
    ready_ticket_count: int = 0
    active_ticket_count: int = 0
    dependency_count: int = 0
    blueprint_count: int = 0
    estimated_minutes: int | None = None

    tags: list[str] | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SpecificationRecord(_RecordBase):
    """A ``specifications`` row (port of ``SpecificationFull`` + architecture §3).

    Content arrays are JSON columns reusing crucible's structured sub-models (the
    ``string[]`` typing in the TS record is stale — the live store holds the full
    structured objects). Drops AI-readiness columns; adds the §3 validator/graph
    cache columns (``last_validator_output``, ``last_gate_result``,
    ``last_validated_at``, ``dependency_tree`` + version/metrics). ``project_id`` is
    the spec's own owning project (kept — it is *not* a child-row denorm).
    """

    id: str
    project_id: str
    title: str
    specification_type_id: str | None = None
    schema_version: str = "1.1"
    description: str | None = None
    background: str | None = None
    status: SpecStatus = SpecStatus.DRAFT
    progress: int = 0

    architecture: str | None = None
    scope: Scope | None = None
    goals: list[Goal] | None = None
    requirements: list[Requirement] | None = None
    non_functional_requirements: list[NonFunctionalRequirement] | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    guardrails: list[Guardrail] | None = None
    tech_stack: list[TechStackItem] | None = None
    folder_structures: list[StructureItem] | None = None
    shared_patterns: list[SharedPattern] | None = None
    epic_targets: EpicTargets | None = None
    field_declarations: dict[str, FieldDeclaration] | None = None
    tags: list[str] | None = None
    estimated_minutes: int | None = None

    # Denormalized count columns (aggregator-owned).
    epic_count: int = 0
    completed_epic_count: int = 0
    todo_epic_count: int = 0
    in_progress_epic_count: int = 0
    ticket_count: int = 0
    completed_ticket_count: int = 0
    pending_ticket_count: int = 0
    ready_ticket_count: int = 0
    active_ticket_count: int = 0
    dependency_count: int = 0
    blueprint_count: int = 0

    # DAG / validator cache columns (architecture §3).
    dependency_tree: dict[str, Any] | None = None
    dependency_tree_version: int | None = None
    dependency_tree_updated_at: str | None = None
    critical_path_length: int | None = None
    max_blocker_depth: int | None = None
    last_validator_output: dict[str, Any] | None = None
    last_gate_result: str | None = None
    last_validated_at: str | None = None

    created_at: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# SpecFull recompose
# ---------------------------------------------------------------------------


def _order_key(order: int | None, id_: str) -> tuple[bool, int, str]:
    """Sort key: by ``order`` ascending (rows without an order last), then ``id``."""
    return (order is None, order if order is not None else 0, id_)


def _build_ticket(
    record: TicketRecord,
    deps_by_ticket: dict[str, list[DependencyLink]],
    refs_by_ticket: dict[str, list[BlueprintReference]],
) -> Ticket:
    # ME.6: the grouped `testSpecification` view is only valid with >=1 test type;
    # a ticket with no planned tests surfaces the field as absent (matches the TS
    # recompose, where an empty view is not emitted).
    test_specification: TestSpecification | None = None
    if record.test_types:
        test_specification = TestSpecification(
            test_types=list(record.test_types),
            quality_gates=list(record.quality_gates),
            test_commands=list(record.test_commands),
            coverage_target=record.coverage_target,
        )

    return Ticket(
        id=record.id,
        epic_id=record.epic_id,
        ticket_number=record.ticket_number,
        title=record.title,
        description=record.description,
        ticket_type=record.ticket_type or TicketType.IMPLEMENTATION,
        complexity=record.complexity or Complexity.MEDIUM,
        estimated_minutes=record.estimated_minutes if record.estimated_minutes is not None else 0,
        order=record.order,
        acceptance_criteria=list(record.acceptance_criteria),
        implementation_steps=list(record.implementation_steps),
        files_to_be_created=list(record.files_to_be_created),
        files_to_be_modified=list(record.files_to_be_modified),
        files_to_be_deleted=list(record.files_to_be_deleted),
        files_to_be_referenced=list(record.files_to_be_referenced),
        test_specification=test_specification,
        code_references=list(record.code_references),
        type_references=list(record.type_references),
        blueprint_references=refs_by_ticket.get(record.id, []),
        dependencies=deps_by_ticket.get(record.id, []),
        field_declarations=record.field_declarations,
    )


def _build_epic(record: EpicRecord, tickets: list[Ticket]) -> Epic:
    return Epic(
        id=record.id,
        specification_id=record.specification_id,
        title=record.title,
        description=record.description,
        objective=record.objective,
        order=record.order,
        estimated_minutes=record.estimated_minutes,
        architecture=record.architecture,
        scope=record.scope,
        goals=record.goals,
        acceptance_criteria=record.acceptance_criteria,
        validation_commands=record.validation_commands,
        api_contracts=record.api_contracts,
        shared_patterns=record.shared_patterns,
        file_structures=record.file_structures,
        requirements_covered=record.requirements_covered,
        nfrs_covered=record.nfrs_covered,
        goals_covered=record.goals_covered,
        field_declarations=record.field_declarations,
        category=record.category,
        tickets=tickets,
    )


def _build_blueprint(record: BlueprintRecord) -> Blueprint:
    return Blueprint(
        id=record.id,
        title=record.title,
        description=record.description,
        slug=record.slug,
        category=record.category,
        format=record.format,
        coverage_type=record.coverage_type,
        content=record.content,
        notes=record.notes,
        version=record.version,
        order=record.order,
        tags=record.tags,
    )


def build_spec_full(
    spec: SpecificationRecord,
    epics: Sequence[EpicRecord],
    tickets: Sequence[TicketRecord],
    blueprints: Sequence[BlueprintRecord],
    dependencies: Sequence[DependencyEdge],
    *,
    blueprint_refs: Sequence[TicketBlueprintRef] | None = None,
) -> Specification:
    """Recompose flat records into crucible's nested :class:`Specification`.

    Epics and their tickets are ordered by ``order`` then ``id``. Each ticket's
    dependency edges become :class:`crucible.models.DependencyLink`\\ s whose
    ``ticket_id`` is the *depended-on* ticket. ``blueprint_refs`` (the
    ticket↔blueprint join rows) become each ticket's ``blueprint_references``.

    The result's ``model_dump(by_alias=True, exclude_none=True)`` is a valid
    crucible validator input.
    """
    deps_by_ticket: dict[str, list[DependencyLink]] = {}
    for edge in sorted(dependencies, key=lambda d: (d.ticket_id, d.depends_on_id)):
        deps_by_ticket.setdefault(edge.ticket_id, []).append(
            DependencyLink(ticket_id=edge.depends_on_id, type=edge.type)
        )

    refs_by_ticket: dict[str, list[BlueprintReference]] = {}
    for ref in sorted(blueprint_refs or [], key=lambda r: (r.ticket_id, r.blueprint_id)):
        refs_by_ticket.setdefault(ref.ticket_id, []).append(
            BlueprintReference(blueprint_id=ref.blueprint_id, context=ref.context, section=ref.section)
        )

    tickets_by_epic: dict[str, list[Ticket]] = {}
    for record in sorted(tickets, key=lambda t: _order_key(t.order, t.id)):
        tickets_by_epic.setdefault(record.epic_id, []).append(
            _build_ticket(record, deps_by_ticket, refs_by_ticket)
        )

    nested_epics = [
        _build_epic(record, tickets_by_epic.get(record.id, []))
        for record in sorted(epics, key=lambda e: _order_key(e.order, e.id))
    ]
    nested_blueprints = [
        _build_blueprint(record)
        for record in sorted(blueprints, key=lambda b: _order_key(b.order, b.id))
    ]

    return Specification(
        id=spec.id,
        project_id=spec.project_id,
        title=spec.title,
        description=spec.description,
        status=spec.status,
        goals=list(spec.goals or []),
        requirements=list(spec.requirements or []),
        architecture=spec.architecture or "",
        scope=spec.scope or Scope(),
        tech_stack=list(spec.tech_stack or []),
        folder_structures=list(spec.folder_structures or []),
        acceptance_criteria=list(spec.acceptance_criteria or []),
        non_functional_requirements=list(spec.non_functional_requirements or []),
        shared_patterns=spec.shared_patterns,
        guardrails=list(spec.guardrails or []),
        background=spec.background,
        epics=nested_epics,
        blueprints=nested_blueprints,
        field_declarations=spec.field_declarations,
        epic_targets=spec.epic_targets,
        estimated_minutes=spec.estimated_minutes,
    )
