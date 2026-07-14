"""Runtime / lifecycle / status enum vocabulary for SpecSmither.

These are the enums that crucible does NOT provide. The OpenSpec *authoring*
vocabulary (TicketType, Complexity, EpicCategory, TestType, …) is the single
source of truth in ``crucible.models.enums`` and is reused from there — it is
deliberately NOT redefined in this module.

Ground truth (ported verbatim so values round-trip with the TS/JSON wire):
- ``session-types/src/runtime/enums.ts`` (lifecycle / planning / cross-val vocab)
- ``api-types/src/runtime/status.ts`` (the 4-value ticket status surface)
- ``api-types/src/runtime/ticket-record.ts`` (ticket file-change ``kind``)

All members are :class:`enum.StrEnum`, so a member compares equal to and is
usable anywhere its verbatim string value is expected.
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
    """Canonical 4-value ticket status (api-types ``TICKET_STATUSES``).

    "Blocked" is not a status — it is signalled via ``block_reason`` on a
    ``pending`` ticket.
    """

    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    DONE = "done"


class SpecStatus(StrEnum):
    """Spec lifecycle (session-types ``SPEC_STATUSES``) — all 8 values verbatim.

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
    """Planning-session status (session-types ``PLANNING_SESSION_STATUSES``)."""

    ACTIVE = "active"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    CLOSED = "closed"


class WorkSessionStatus(StrEnum):
    """Work-session status (session-types ``WORK_SESSION_STATUSES``)."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CLOSED = "closed"


class PlanningPhase(StrEnum):
    """Planning phase machine (session-types ``PLANNING_PHASES``)."""

    PLANNING_SPEC = "planning_spec"
    EPIC_DECOMPOSITION = "epic_decomposition"
    EPIC_EXPANSION = "epic_expansion"
    TICKET_DECOMPOSITION = "ticket_decomposition"
    TICKET_EXPANSION = "ticket_expansion"
    CROSS_VALIDATION = "cross_validation"
    PLANNED = "planned"


class TransitionTrigger(StrEnum):
    """Phase-transition trigger (session-types ``TRANSITION_TRIGGERS``)."""

    AUTO_INITIAL = "auto_initial"
    AI_AGENT = "ai_agent"
    HUMAN_APPROVE = "human_approve"
    HUMAN_COMPLETE = "human_complete"
    HUMAN_REJECT_WITH_FEEDBACK = "human_reject_with_feedback"
    HUMAN_REJECT_NO_FEEDBACK = "human_reject_no_feedback"


class Outcome(StrEnum):
    """Audited action outcome (session-types ``OUTCOMES``)."""

    SUCCESS = "success"
    DENIED = "denied"


class DatapointTrigger(StrEnum):
    """Score-datapoint trigger (session-types ``DATAPOINT_TRIGGERS``)."""

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
    """Guidance-message variant (session-types ``GUIDANCE_VARIANTS``).

    Includes the six M6.6 additions consumed exclusively by
    ``get_planning_status``.
    """

    DENIED = "denied"
    GATE_FAILED = "gate_failed"
    GATE_PASSED = "gate_passed"
    HUMAN_HANDOVER = "human_handover"
    HUMAN_FEEDBACK = "human_feedback"
    PHASE_ADVANCE = "phase_advance"
    PHASE_ROLLBACK = "phase_rollback"
    PHASE_COMPLETE = "phase_complete"
    # M6.6 additions.
    PHASE_STATUS_REPORT = "phase_status_report"
    AWAITING_HUMAN_REVIEW_HANDOVER = "awaiting_human_review_handover"
    HUMAN_FEEDBACK_RECEIVED = "human_feedback_received"
    HUMAN_REJECTED_NO_FEEDBACK = "human_rejected_no_feedback"
    PHASE_ADVANCED_AFTER_APPROVE = "phase_advanced_after_approve"
    SESSION_CLOSED = "session_closed"


class FindingCategory(StrEnum):
    """Cross-validation finding category (session-types ``FINDING_CATEGORIES``)."""

    RUBRIC = "rubric"
    COUNT = "count"
    CROSS_CUT = "cross-cut"
    CASCADE = "cascade"
    CROSS_VALIDATION = "cross-validation"
    SCHEMA = "schema"


class GateResult(StrEnum):
    """Gate result (session-types ``GATE_RESULTS``)."""

    PASS = "pass"
    FAIL = "fail"


class FieldState(StrEnum):
    """Field-presence state (session-types ``FIELD_STATES``)."""

    EMPTY = "empty"
    PARTIAL = "partial"
    FILLED = "filled"
    NA = "na"


class ExpectedAction(StrEnum):
    """Planned file action (session-types ``EXPECTED_ACTIONS``)."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    REFERENCE = "reference"


class ActualAction(StrEnum):
    """Observed file action (session-types ``ACTUAL_ACTIONS``)."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    REFERENCED = "referenced"
    ABSENT = "absent"


class FileChangeStatus(StrEnum):
    """Expected-vs-actual reconciliation (session-types ``FILE_CHANGE_STATUSES``)."""

    MATCHED = "matched"
    MISSING = "missing"
    MISMATCHED = "mismatched"
    EXTRA = "extra"


class JustificationApproved(StrEnum):
    """Justification approval state (session-types ``JUSTIFICATION_APPROVED_VALUES``)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class FileChangeKind(StrEnum):
    """Ticket PLAN-side file-change kind (api-types ``TicketFileChangeKind``).

    Drives ``ticket_file_changes.kind`` (schema §3). Distinct from the work-side
    :class:`ExpectedAction` / :class:`ActualAction` vocabulary — these are the
    ``toBeX`` camelCase literals from ``api-types/runtime/ticket-record.ts``.
    """

    TO_BE_CREATED = "toBeCreated"
    TO_BE_MODIFIED = "toBeModified"
    TO_BE_DELETED = "toBeDeleted"
    TO_BE_REFERENCED = "toBeReferenced"


class EpicStatus(StrEnum):
    """Epic lifecycle status.

    A DISTINCT enum exists in the TS (``types/src/schema/epic.ts`` →
    ``'todo' | 'in_progress' | 'completed'``), and the planning verbs that
    SpecSmither ports create epics with ``status: 'todo'`` and explicitly comment
    that this is "a constrained EpicStatus enum (todo|in_progress|completed)"
    (``lifecycle/.../action-planning-session.ts``). So epics do NOT share the
    ticket vocabulary — these three values are ported verbatim.
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ActorType(StrEnum):
    """Audit actor identity (§6 actor model).

    Confirmed against the TS: ``actor: 'agent' | 'human'`` throughout the
    planning verbs / ``action-builder.ts``. There is no named ``ACTOR_TYPES``
    array — the union is inlined — so the two members are defined here. MCP tool
    calls resolve to ``agent``; explicit CLI handover/approve commands to ``human``.
    """

    AGENT = "agent"
    HUMAN = "human"
