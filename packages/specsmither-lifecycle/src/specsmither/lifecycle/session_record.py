"""The pure planning-session STATE record the verb surface reads (``PlanningSessionRecord``).

This is the DB-free counterpart of the SQLAlchemy ORM
:class:`~specsmither.db.models.planning.PlanningSession`. The pure lifecycle verbs
only ever **read** a session row (~a dozen attributes) or construct a transient
post-state view of it; they never mutate a loaded row and never flush. Holding
that state in a frozen dataclass — instead of the ORM instance — is what lets the
whole verb surface import without pulling ``sqlalchemy`` onto the pure path (the
last of the extraction's coupling sinks).

The concrete SQLite :class:`~specsmither.lifecycle.ports.PlanningSessionStore`
adapter maps an ORM ``PlanningSession`` row to this record before handing it to a
verb (``specsmither.adapters.lifecycle_ports``); the WritePlan persist path is
unaffected because every ``sessionUpdate`` carries plain field dicts, not this
record.

Field alignment: every attribute the pure surface reads (``compose``, the CPS gate,
``inspect``, ``get_planning_status``) OR constructs (``start`` /
``echo_session`` / ``post_state_view``) is present here with the same name the ORM
column uses, so the ORM→record mapping is a straight attribute copy. ``id`` /
``specification_id`` / ``status`` / ``current_phase`` are always supplied by every
construction site; the rest default to the value an un-flushed ORM row would read
back (``None`` / ``0``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["PlanningSessionRecord"]


@dataclass(frozen=True)
class PlanningSessionRecord:
    """A pure, frozen snapshot of a ``planning_sessions`` row (the verb STATE object).

    Mirrors :class:`~specsmither.db.models.planning.PlanningSession` field-for-field
    for everything the pure lifecycle surface touches. Statuses / phases / triggers
    are the exact ``StrEnum`` ``.value`` strings (the same loose ``str`` the ORM
    stores), so the record round-trips with the JSON wire exactly as the row did.

    ``last_validator_output`` is the persisted gate-output blob (shape varies →
    :data:`~typing.Any`); ``inspect`` rehydrates it for display only. The JSON
    snapshot columns (``pending_human_feedback`` / ``last_process_guidance`` /
    ``last_lifecycle_planning_guidance`` / ``session_metadata``) are carried as
    plain ``dict``\\ s.
    """

    # Always supplied by every construction site (no ORM default is ever observed).
    id: str
    specification_id: str
    status: str
    current_phase: str

    # Lifecycle timestamps (ISO-8601 strings; ``None`` when never set).
    started_at: str | None = None
    last_action_at: str | None = None
    completed_at: str | None = None
    closed_at: str | None = None

    # Cached score / gate + the op counter.
    last_score: float | None = None
    actions_count: int = 0

    # Resume model: {content, recordedAt, recordedByUserId} | None.
    pending_human_feedback: dict[str, Any] | None = None

    # Latest transition / read / validation bookkeeping.
    last_transition_trigger: str | None = None
    last_transition_at: str | None = None
    last_read_at: str | None = None
    last_validated_at: str | None = None
    last_gate_result: str | None = None

    # The persisted gate-output blob (holds ``validated_phase``); shape varies.
    last_validator_output: Any = None

    # Latest-only denormalized guidance snapshots (read for inspect / status display).
    last_process_guidance: dict[str, Any] | None = None
    last_lifecycle_planning_guidance: dict[str, Any] | None = None
    session_metadata: dict[str, Any] | None = None
