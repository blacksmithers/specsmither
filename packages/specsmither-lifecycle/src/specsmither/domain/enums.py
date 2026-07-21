"""Runtime / lifecycle / status enum vocabulary for SpecSmither.

These are the enums that crucible does NOT provide. The OpenSpec *authoring*
vocabulary (TicketType, Complexity, EpicCategory, TestType, …) is the single
source of truth in ``crucible.models.enums`` and is reused from there — it is
deliberately NOT redefined in this module.

These are the canonical wire vocabularies (values round-trip as their plain JSON
string):
- the lifecycle / planning / cross-validation vocabulary
- the 4-value ticket status surface
- the ticket file-change ``kind``

All members are :class:`enum.StrEnum`, so a member compares equal to and is
usable anywhere its string value is expected.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ActorType",
    "ActualAction",
    "DatapointTrigger",
    "EpicStatus",
    "ExpectedAction",
    "FieldState",
    "FileChangeKind",
    "FileChangeStatus",
    "FindingCategory",
    "GateResult",
    "GuidanceVariant",
    "JustificationApproved",
    "Outcome",
    "PlanningPhase",
    "PlanningSessionStatus",
    "SpecStatus",
    "TicketStatus",
    "TransitionTrigger",
    "WorkSessionStatus",
]


class TicketStatus(StrEnum):
    """Canonical 4-value ticket status (``TICKET_STATUSES``).

    "Blocked" is not a status — it is signalled via ``block_reason`` on a
    ``pending`` ticket.
    """

    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    DONE = "done"


class SpecStatus(StrEnum):
    """Spec lifecycle (``SPEC_STATUSES``) — all 8 canonical values.

    SpecSmither has NO review lifecycle, so ``ready_for_review`` / ``in_review``
    / ``reviewed`` are UNREACHABLE here. They are kept because the M0 CRUD freeze
    guards (#18) reference them: ``reviewed`` is treated as read-only and
    ``in_review`` blocks create. Do not drop them.
    """

    DRAFT = "draft"
    PLANNING = "planning"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    # Unreachable in SpecSmither (no review lifecycle) — retained for the
    # CRUD-freeze guards (#18): `reviewed` = read-only, `in_review` = blocks create.
    READY_FOR_REVIEW = "ready_for_review"
    IN_REVIEW = "in_review"
    REVIEWED = "reviewed"
    DONE = "done"


class PlanningSessionStatus(StrEnum):
    """Planning-session status (``PLANNING_SESSION_STATUSES``)."""

    ACTIVE = "active"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    CLOSED = "closed"


class WorkSessionStatus(StrEnum):
    """Work-session status (``WORK_SESSION_STATUSES``)."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CLOSED = "closed"


class PlanningPhase(StrEnum):
    """Planning phase machine (``PLANNING_PHASES``)."""

    PLANNING_SPEC = "planning_spec"
    EPIC_DECOMPOSITION = "epic_decomposition"
    EPIC_EXPANSION = "epic_expansion"
    TICKET_DECOMPOSITION = "ticket_decomposition"
    TICKET_EXPANSION = "ticket_expansion"
    CROSS_VALIDATION = "cross_validation"
    PLANNED = "planned"


class TransitionTrigger(StrEnum):
    """Phase-transition trigger (``TRANSITION_TRIGGERS``)."""

    AUTO_INITIAL = "auto_initial"
    AI_AGENT = "ai_agent"
    HUMAN_APPROVE = "human_approve"
    HUMAN_COMPLETE = "human_complete"
    HUMAN_REJECT_WITH_FEEDBACK = "human_reject_with_feedback"
    HUMAN_REJECT_NO_FEEDBACK = "human_reject_no_feedback"


class Outcome(StrEnum):
    """Audited action outcome (``OUTCOMES``)."""

    SUCCESS = "success"
    DENIED = "denied"


class DatapointTrigger(StrEnum):
    """Score-datapoint trigger (``DATAPOINT_TRIGGERS``)."""

    CREATED = "created"
    METADATA_UPDATED = "metadata_updated"
    EPIC_FIELD_UPDATED = "epic_field_updated"
    TICKET_FIELD_UPDATED = "ticket_field_updated"
    BLUEPRINT_FIELD_UPDATED = "blueprint_field_updated"
    DEPENDENCY_ADDED = "dependency_added"
    DEPENDENCY_REMOVED = "dependency_removed"
    BLUEPRINT_LINKED = "blueprint_linked"
    BLUEPRINT_UNLINKED = "blueprint_unlinked"
    CROSS_VAL_RECOMPUTE = "cross_val_recompute"


class GuidanceVariant(StrEnum):
    """Guidance-message variant (``GUIDANCE_VARIANTS``).

    Includes the six additions consumed exclusively by ``get_planning_status``.
    """

    DENIED = "denied"
    GATE_FAILED = "gate_failed"
    GATE_PASSED = "gate_passed"
    HUMAN_HANDOVER = "human_handover"
    HUMAN_FEEDBACK = "human_feedback"
    PHASE_ADVANCE = "phase_advance"
    PHASE_ROLLBACK = "phase_rollback"
    PHASE_COMPLETE = "phase_complete"
    # Status-poll additions.
    PHASE_STATUS_REPORT = "phase_status_report"
    AWAITING_HUMAN_REVIEW_HANDOVER = "awaiting_human_review_handover"
    HUMAN_FEEDBACK_RECEIVED = "human_feedback_received"
    HUMAN_REJECTED_NO_FEEDBACK = "human_rejected_no_feedback"
    PHASE_ADVANCED_AFTER_APPROVE = "phase_advanced_after_approve"
    SESSION_CLOSED = "session_closed"


class FindingCategory(StrEnum):
    """Cross-validation finding category (``FINDING_CATEGORIES``)."""

    RUBRIC = "rubric"
    COUNT = "count"
    CROSS_CUT = "cross-cut"
    CASCADE = "cascade"
    CROSS_VALIDATION = "cross-validation"
    SCHEMA = "schema"


class GateResult(StrEnum):
    """Gate result (``GATE_RESULTS``)."""

    PASS = "pass"
    FAIL = "fail"


class FieldState(StrEnum):
    """Field-presence state (``FIELD_STATES``)."""

    EMPTY = "empty"
    PARTIAL = "partial"
    FILLED = "filled"
    NA = "na"


class ExpectedAction(StrEnum):
    """Planned file action (``EXPECTED_ACTIONS``)."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    REFERENCE = "reference"


class ActualAction(StrEnum):
    """Observed file action (``ACTUAL_ACTIONS``)."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    REFERENCED = "referenced"
    ABSENT = "absent"


class FileChangeStatus(StrEnum):
    """Expected-vs-actual reconciliation (``FILE_CHANGE_STATUSES``)."""

    MATCHED = "matched"
    MISSING = "missing"
    MISMATCHED = "mismatched"
    EXTRA = "extra"


class JustificationApproved(StrEnum):
    """Justification approval state (``JUSTIFICATION_APPROVED_VALUES``)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FileChangeKind(StrEnum):
    """Ticket PLAN-side file-change kind (``TicketFileChangeKind``).

    Drives ``ticket_file_changes.kind`` (schema §3). Distinct from the work-side
    :class:`ExpectedAction` / :class:`ActualAction` vocabulary — these are the
    ``toBeX`` camelCase literals.
    """

    TO_BE_CREATED = "toBeCreated"
    TO_BE_MODIFIED = "toBeModified"
    TO_BE_DELETED = "toBeDeleted"
    TO_BE_REFERENCED = "toBeReferenced"


class EpicStatus(StrEnum):
    """Epic lifecycle status.

    Epics use a DISTINCT, constrained vocabulary
    (``'todo' | 'in_progress' | 'completed'``): the planning verbs create epics with
    ``status: 'todo'``. So epics do NOT share the ticket vocabulary — these are the
    three canonical values.
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ActorType(StrEnum):
    """Audit actor identity (§6 actor model).

    The actor is ``'agent' | 'human'`` throughout the planning verbs and the audit
    row builders. There is no named ``ACTOR_TYPES`` vocabulary — the union is
    inlined — so the two members are defined here. MCP tool calls resolve to
    ``agent``; explicit CLI handover/approve commands to ``human``.
    """

    AGENT = "agent"
    HUMAN = "human"
