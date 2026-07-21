"""Phase-transition row builder.

A **pure** factory for the ``planning_phase_transitions`` row payload that an L4
verb wraps in a :class:`~specsmither.adapters.write_plan_executor.RecordTransition`
WritePlan item. The M0 executor's ``_filtered`` lands the snake_case keys onto the
ORM columns and coerces any enum value to its ``.value`` string.

The persisted transition shape (``db/models/planning.py``) carries no per-user or
per-transition prose (single local user); an ``actor`` column records who drove the
transition, and ``created_at`` is the append-only timestamp.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from specsmither.domain.enums import ActorType, PlanningPhase, TransitionTrigger
from specsmither.ids import new_ulid, now_iso
from specsmither.lifecycle.ports import Clock, IdGenerator

__all__ = [
    "build_transition",
]


def _v(value: Any) -> Any:
    """Coerce an :class:`enum.Enum` member to its plain ``.value`` (passthrough else)."""
    return value.value if isinstance(value, Enum) else value


def _mint(id_generator: IdGenerator | None) -> str:
    return (id_generator or new_ulid)()


def _now(clock: Clock | None) -> str:
    return now_iso() if clock is None else clock().isoformat()


def build_transition(
    *,
    session_id: str,
    from_phase: PlanningPhase | str,
    to_phase: PlanningPhase | str,
    trigger: TransitionTrigger | str,
    actor: ActorType | str,
    id_generator: IdGenerator | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Build a ``planning_phase_transitions`` row dict.

    The returned dict is the payload for a ``RecordTransition`` WritePlan item. ``id``
    is a fresh ULID (or ``id_generator()``); ``created_at`` is an ISO-8601 string from
    ``now_iso`` (or ``clock()``, injected for determinism). ``from_phase`` /
    ``to_phase`` / ``trigger`` / ``actor`` are stored as their plain enum ``.value``
    strings.
    """

    return {
        "id": _mint(id_generator),
        "planning_session_id": session_id,
        "from_phase": _v(from_phase),
        "to_phase": _v(to_phase),
        "trigger": _v(trigger),
        "actor": _v(actor),
        "created_at": _now(clock),
    }
