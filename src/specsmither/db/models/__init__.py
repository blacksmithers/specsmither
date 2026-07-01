"""ORM model aggregation point — importing this package registers the whole schema.

This module imports **every** model sub-module
(:mod:`~specsmither.db.models.core`, :mod:`~specsmither.db.models.ticket_children`,
:mod:`~specsmither.db.models.planning`, :mod:`~specsmither.db.models.work`) so that
the single declarative :class:`~specsmither.db.base.Base` registry is fully
populated the moment ``specsmither.db.models`` is imported. ``apply_migrations`` and
every repository import from here (and only here), so there is exactly one place
that guarantees all model tables are attached to ``Base.metadata``.

It re-exports :class:`Base` plus every concrete ORM model class and the
``new_ulid`` / ``now_iso`` id/timestamp helpers for caller convenience.

The schema is: 7 core entities + git (2) + spec-type (1) + ticket-child (6:
acceptance criteria, implementation steps, file changes, planned tests, code/type
snippets) + planning-session (5) + work-session (5) + config (1) = 27 tables. No
``review_*`` tables (dropped per the SpecSmither architecture).
"""

from __future__ import annotations

from specsmither.db.base import Base, new_ulid, now_iso
from specsmither.db.models.config import PlanningConfig
from specsmither.db.models.core import (
    Blueprint,
    Epic,
    EpicCommit,
    Project,
    Specification,
    SpecificationPr,
    SpecificationType,
    Ticket,
    TicketBlueprintRef,
    TicketDependency,
)
from specsmither.db.models.planning import (
    PlanningEntityScoreDatapoint,
    PlanningPhaseTransition,
    PlanningSession,
    PlanningSessionAction,
    PlanningSessionAggregate,
)
from specsmither.db.models.ticket_children import (
    AcceptanceCriterion,
    CodeSnippet,
    ImplementationStep,
    TicketFileChange,
    TicketTest,
    TypeSnippet,
)
from specsmither.db.models.work import (
    WorkSession,
    WorkSessionAcceptanceCheck,
    WorkSessionFileChange,
    WorkSessionImplStepCompletion,
    WorkSessionTestResult,
)

__all__ = [
    "AcceptanceCriterion",
    "Base",
    "Blueprint",
    "CodeSnippet",
    "Epic",
    "EpicCommit",
    "ImplementationStep",
    "PlanningConfig",
    "PlanningEntityScoreDatapoint",
    "PlanningPhaseTransition",
    "PlanningSession",
    "PlanningSessionAction",
    "PlanningSessionAggregate",
    "Project",
    "Specification",
    "SpecificationPr",
    "SpecificationType",
    "Ticket",
    "TicketBlueprintRef",
    "TicketDependency",
    "TicketFileChange",
    "TicketTest",
    "TypeSnippet",
    "WorkSession",
    "WorkSessionAcceptanceCheck",
    "WorkSessionFileChange",
    "WorkSessionImplStepCompletion",
    "WorkSessionTestResult",
    "new_ulid",
    "now_iso",
]
