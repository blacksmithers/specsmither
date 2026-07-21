"""Core entity ORM models: the Specification → Epic → Ticket spine + git + spec-type.

The SpecSmither persistence schema (architecture §3): local-first, so cloud-only
fields have no place here (there is no ``contentS3Key``, no ``projectId`` denorm on
child rows, no S3 / GSI / auth / readiness-AI fields, no ``review_*`` tables).
Blueprint bodies are stored *inline* (``content``). The denormalized count columns
are kept (declared here, written later by the recompute worklist).

Status / enum-valued columns are plain ``String`` columns typed ``Mapped[str]`` and
default to the corresponding enum's ``.value`` — statuses are unconstrained strings
on the wire, so no CHECK / native-enum constraint is imposed. JSON content lives in
:class:`~specsmither.db.base.JSONType` columns typed as their decoded Python shape.

Every child→parent foreign key is ``ON DELETE CASCADE`` (the cascade is the
contract). The optional parent→children ``relationship()`` declarations use
``cascade="all, delete-orphan"`` + ``passive_deletes=True`` so ``session.delete(parent)``
defers to the database cascade. The child-backed ticket arrays (acceptance criteria,
implementation steps, file changes, tests, code/type snippets) and the session
tables live in sibling modules.
"""

from __future__ import annotations

from typing import Any

from crucible.models.enums import BlueprintCoverageType, DependencyType
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from specsmither.db.base import Base, IdMixin, JSONType, TimestampMixin, now_iso
from specsmither.domain.enums import EpicStatus, SpecStatus, TicketStatus

__all__ = [
    "Blueprint",
    "Epic",
    "EpicCommit",
    "Project",
    "Specification",
    "SpecificationPr",
    "SpecificationType",
    "Ticket",
    "TicketBlueprintRef",
    "TicketDependency",
]


class Project(IdMixin, TimestampMixin, Base):
    """Root container. Owns specifications; carries denormalized rollup counts.

    Project ``progress`` is *not* persisted (it is derived read-side) — so there is
    deliberately no ``progress`` column here.
    """

    __tablename__ = "projects"

    user_id: Mapped[str] = mapped_column(default="local")
    name: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str | None]

    # Denormalized count columns (recompute-worklist owned).
    spec_count: Mapped[int] = mapped_column(default=0)
    completed_spec_count: Mapped[int] = mapped_column(default=0)
    draft_spec_count: Mapped[int] = mapped_column(default=0)
    planning_spec_count: Mapped[int] = mapped_column(default=0)
    ready_spec_count: Mapped[int] = mapped_column(default=0)
    in_progress_spec_count: Mapped[int] = mapped_column(default=0)
    in_review_spec_count: Mapped[int] = mapped_column(default=0)
    epic_count: Mapped[int] = mapped_column(default=0)
    completed_epic_count: Mapped[int] = mapped_column(default=0)
    ticket_count: Mapped[int] = mapped_column(default=0)
    completed_ticket_count: Mapped[int] = mapped_column(default=0)

    specifications: Mapped[list[Specification]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SpecificationType(IdMixin, Base):
    """A spec template/type. One row is seeded as the default (``is_default``)."""

    __tablename__ = "specification_types"

    name: Mapped[str]
    description: Mapped[str | None]
    is_default: Mapped[bool] = mapped_column(default=False)
    # Append-only: created_at only (no updated_at mixin).
    created_at: Mapped[str] = mapped_column(default=now_iso)


class Specification(IdMixin, TimestampMixin, Base):
    """A specification: the planning unit. Carries content JSON, rollup counts, and
    the cached dependency-tree / validator-output / graph-metric columns (§3)."""

    __tablename__ = "specifications"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    specification_type_id: Mapped[str | None] = mapped_column(
        ForeignKey("specification_types.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(default=SpecStatus.DRAFT.value)
    schema_version: Mapped[str] = mapped_column(default="1.1")
    title: Mapped[str]
    description: Mapped[str | None]
    background: Mapped[str | None]
    architecture: Mapped[str] = mapped_column(default="")

    # Structured content JSON (decoded shapes mirror crucible's sub-models).
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tech_stack: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    folder_structures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    shared_patterns: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    field_declarations: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    goals: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    requirements: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    non_functional_requirements: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    acceptance_criteria: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    guardrails: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    epic_targets: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tags: Mapped[list[str] | None] = mapped_column(JSONType)

    estimated_minutes: Mapped[int | None]
    progress: Mapped[int] = mapped_column(default=0)

    # Denormalized count columns (recompute-worklist owned).
    epic_count: Mapped[int] = mapped_column(default=0)
    completed_epic_count: Mapped[int] = mapped_column(default=0)
    todo_epic_count: Mapped[int] = mapped_column(default=0)
    in_progress_epic_count: Mapped[int] = mapped_column(default=0)
    ticket_count: Mapped[int] = mapped_column(default=0)
    completed_ticket_count: Mapped[int] = mapped_column(default=0)
    pending_ticket_count: Mapped[int] = mapped_column(default=0)
    ready_ticket_count: Mapped[int] = mapped_column(default=0)
    active_ticket_count: Mapped[int] = mapped_column(default=0)
    dependency_count: Mapped[int] = mapped_column(default=0)
    blueprint_count: Mapped[int] = mapped_column(default=0)

    # Cached DAG / validator outputs (graph metrics materialized for fast reads).
    dependency_tree: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    dependency_tree_version: Mapped[int] = mapped_column(default=0)
    dependency_tree_updated_at: Mapped[str | None]
    critical_path_length: Mapped[int] = mapped_column(default=0)
    max_blocker_depth: Mapped[int] = mapped_column(default=0)
    last_validator_output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    last_gate_result: Mapped[str | None]
    last_validated_at: Mapped[str | None]

    epics: Mapped[list[Epic]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    blueprints: Mapped[list[Blueprint]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Epic(IdMixin, TimestampMixin, Base):
    """An epic: a coherent slice of a specification. Owns tickets; carries content
    JSON and ticket-rollup counts."""

    __tablename__ = "epics"

    specification_id: Mapped[str] = mapped_column(
        ForeignKey("specifications.id", ondelete="CASCADE"),
    )
    epic_number: Mapped[int]
    title: Mapped[str]
    description: Mapped[str]
    objective: Mapped[str]
    order: Mapped[int | None]
    status: Mapped[str] = mapped_column(default=EpicStatus.TODO.value)
    category: Mapped[str | None]
    planning_type: Mapped[str | None]
    architecture: Mapped[str | None]

    # Structured content JSON.
    scope: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    goals: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    acceptance_criteria: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    validation_commands: Mapped[list[str] | None] = mapped_column(JSONType)
    api_contracts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    shared_patterns: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    file_structures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    requirements_covered: Mapped[list[str] | None] = mapped_column(JSONType)
    nfrs_covered: Mapped[list[str] | None] = mapped_column(JSONType)
    goals_covered: Mapped[list[str] | None] = mapped_column(JSONType)
    field_declarations: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tags: Mapped[list[str] | None] = mapped_column(JSONType)

    progress: Mapped[int] = mapped_column(default=0)
    estimated_minutes: Mapped[int | None]

    # Denormalized count columns (recompute-worklist owned).
    ticket_count: Mapped[int] = mapped_column(default=0)
    completed_ticket_count: Mapped[int] = mapped_column(default=0)
    pending_ticket_count: Mapped[int] = mapped_column(default=0)
    ready_ticket_count: Mapped[int] = mapped_column(default=0)
    active_ticket_count: Mapped[int] = mapped_column(default=0)
    dependency_count: Mapped[int] = mapped_column(default=0)
    blueprint_count: Mapped[int] = mapped_column(default=0)

    tickets: Mapped[list[Ticket]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Ticket(IdMixin, TimestampMixin, Base):
    """A ticket: the unit of work. Carries flat test fields + code/type refs as JSON;
    its acceptance criteria / implementation steps / file changes / tests are child
    tables (sibling module). ``current_work_session_id`` is a plain string (deliberately
    *not* a FK, to avoid a tickets↔work_sessions cycle)."""

    __tablename__ = "tickets"

    epic_id: Mapped[str] = mapped_column(
        ForeignKey("epics.id", ondelete="CASCADE"),
    )
    ticket_number: Mapped[int | None]
    title: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str] = mapped_column(default=TicketStatus.PENDING.value)
    ticket_type: Mapped[str | None]
    complexity: Mapped[str | None]
    estimated_minutes: Mapped[int] = mapped_column(default=0)
    planning_type: Mapped[str | None]
    block_reason: Mapped[str | None]
    progress: Mapped[int] = mapped_column(default=0)
    order: Mapped[int | None]
    current_work_session_id: Mapped[str | None]

    # Flat test fields + code/type refs + tags as JSON.
    quality_gates: Mapped[list[str] | None] = mapped_column(JSONType)
    test_commands: Mapped[list[str] | None] = mapped_column(JSONType)
    coverage_target: Mapped[float | None] = mapped_column(JSONType)
    code_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    type_references: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    field_declarations: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tags: Mapped[list[str] | None] = mapped_column(JSONType)

    # Denormalized count columns (recompute-worklist owned).
    incoming_dep_count: Mapped[int] = mapped_column(default=0)
    outgoing_dep_count: Mapped[int] = mapped_column(default=0)
    blueprint_count: Mapped[int] = mapped_column(default=0)

    dependencies: Mapped[list[TicketDependency]] = relationship(
        foreign_keys="TicketDependency.ticket_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    blueprint_refs: Mapped[list[TicketBlueprintRef]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Blueprint(IdMixin, TimestampMixin, Base):
    """A blueprint (diagram / ADR / design doc) attached to a specification. The body
    is stored inline in ``content`` (``contentS3Key`` is dropped). ``status`` is a
    DB-only publication state (draft|review|approved|deprecated)."""

    __tablename__ = "blueprints"

    specification_id: Mapped[str] = mapped_column(
        ForeignKey("specifications.id", ondelete="CASCADE"),
    )
    category: Mapped[str]
    title: Mapped[str]
    description: Mapped[str | None]
    slug: Mapped[str | None]
    format: Mapped[str | None]
    coverage_type: Mapped[str] = mapped_column(default=BlueprintCoverageType.TICKET.value)
    content: Mapped[str]
    notes: Mapped[str | None]
    version: Mapped[str | None]
    order: Mapped[int | None]
    status: Mapped[str] = mapped_column(default="draft")
    tags: Mapped[list[str] | None] = mapped_column(JSONType)


class TicketDependency(IdMixin, Base):
    """A directed dependency edge ``ticket → depends_on``. Both endpoints are tickets;
    *both* foreign keys cascade. Append-only (``created_at`` only)."""

    __tablename__ = "ticket_dependencies"
    __table_args__ = (UniqueConstraint("ticket_id", "depends_on_id"),)

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
    )
    type: Mapped[str] = mapped_column(default=DependencyType.REQUIRES.value)
    created_at: Mapped[str] = mapped_column(default=now_iso)


class TicketBlueprintRef(IdMixin, Base):
    """A ticket↔blueprint join row. Append-only (``created_at`` only)."""

    __tablename__ = "ticket_blueprint_refs"
    __table_args__ = (UniqueConstraint("ticket_id", "blueprint_id"),)

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"),
    )
    blueprint_id: Mapped[str] = mapped_column(
        ForeignKey("blueprints.id", ondelete="CASCADE"),
    )
    context: Mapped[str | None]
    section: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(default=now_iso)


class SpecificationPr(IdMixin, TimestampMixin, Base):
    """A linked pull request for a specification. ``UNIQUE(specification_id, pr_number)``
    makes ``link_pull_request`` idempotent."""

    __tablename__ = "specification_prs"
    __table_args__ = (UniqueConstraint("specification_id", "pr_number"),)

    specification_id: Mapped[str] = mapped_column(
        ForeignKey("specifications.id", ondelete="CASCADE"),
    )
    pr_number: Mapped[int]
    url: Mapped[str | None]
    title: Mapped[str | None]
    state: Mapped[str | None]


class EpicCommit(IdMixin, Base):
    """A git commit linked to an epic. Append-only (``created_at`` only)."""

    __tablename__ = "epic_commits"

    epic_id: Mapped[str] = mapped_column(
        ForeignKey("epics.id", ondelete="CASCADE"),
    )
    commit_hash: Mapped[str]
    message: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(default=now_iso)
