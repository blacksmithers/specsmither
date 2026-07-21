"""The per-operation phase-guard table — the 17-op registry + classifier.

Pure: a static
data table plus two functions, no I/O, no dependencies beyond the
:class:`~specsmither.domain.enums.PlanningPhase` vocabulary.

:data:`OPERATIONS` maps every :data:`PlanningOperationName` to an
:class:`OperationDef` (17 real ops: 16 mutating + 1 read-only) or to ``None``
(the 9 synthetic / audit-only ops, which have no callable definition). Each
:class:`OperationDef` records the operation's *home* phase (``native_phase``),
the phases in which it is outright rejected (``forbidden_phases``), an optional
``max_batch`` cap, optional pre-condition ``guards``, and whether it tolerates
concurrent actors (``multi_actor``).

:func:`classify_operation_call` is the core decision the verbs consult before
running an operation: ``forbidden`` (reject), ``native`` (home phase — proceed),
or ``late`` (allowed, but the caller is working past the native phase, which
triggers a phase rollback elsewhere in the lifecycle).

Notes on values that affect outputs:

* ``create_dependencies`` carries ``max_batch == 5000`` (**not**
  100): real planning sessions ship a full dependency graph in one shot, and the
  batch pre-check dedups + cycle-checks before any write.
* ``delete_epic`` / ``delete_ticket`` carry a ``minCount`` guard (at least one
  must remain); ``delete_blueprint`` carries a ``blueprintEpicRatio`` guard.
* ``create_blueprint`` has **no** ratio guard — the blueprint:epic ratio is a
  delete-side hard-deny only (locked decision; no create-side check).
* ``get_planning_status`` is native in every phase (``native_phase == 'any'``),
  never forbidden, and is the only ``multi_actor`` operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from specsmither.domain.enums import PlanningPhase

__all__ = [
    "OPERATIONS",
    "PLANNING_MUTATING_OPERATIONS",
    "PLANNING_READ_ONLY_OPERATIONS",
    "PLANNING_SYNTHETIC_OPERATIONS",
    "GuardSpec",
    "NativePhase",
    "OperationCallClass",
    "OperationDef",
    "OperationKind",
    "PlanningOperationName",
    "classify_operation_call",
    "get_operation_def",
]

# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #

#: Every planning operation name (mutating + read-only + synthetic).
PlanningOperationName = Literal[
    # mutating
    "update_spec",
    "create_epic",
    "update_epic",
    "delete_epic",
    "create_ticket",
    "update_ticket",
    "delete_ticket",
    "create_blueprint",
    "update_blueprint",
    "delete_blueprint",
    "link_blueprint_to_tickets",
    "unlink_blueprint_to_tickets",
    "create_dependencies",
    "delete_dependencies",
    "justify",
    "unjustify",
    # read-only
    "get_planning_status",
    # synthetic / audit-only
    "start_planning_session",
    "complete_planning_session",
    "phase_complete",
    "phase_advance",
    "phase_rollback",
    "human_approve",
    "human_reject_with_feedback",
    "human_reject_no_feedback",
    "session_closed",
]

OperationKind = Literal["mutating", "read-only"]
"""Whether an operation writes (``mutating``) or only reads (``read-only``)."""

GuardType = Literal["minCount", "blueprintEpicRatio"]
"""Identifier of a pre-condition guard enforced before an operation is accepted."""

NativePhase = PlanningPhase | Literal["any"]
"""An operation's home phase, or ``'any'`` for phase-agnostic operations."""

OperationCallClass = Literal["forbidden", "native", "late"]
"""Classification of an ``(op, phase)`` call — see :func:`classify_operation_call`."""

#: The 16 mutating planning operations (session-types
#: ``PLANNING_MUTATING_OPERATIONS``).
PLANNING_MUTATING_OPERATIONS: Final[tuple[PlanningOperationName, ...]] = (
    "update_spec",
    "create_epic",
    "update_epic",
    "delete_epic",
    "create_ticket",
    "update_ticket",
    "delete_ticket",
    "create_blueprint",
    "update_blueprint",
    "delete_blueprint",
    "link_blueprint_to_tickets",
    "unlink_blueprint_to_tickets",
    "create_dependencies",
    "delete_dependencies",
    "justify",
    "unjustify",
)

#: The single read-only planning operation (session-types
#: ``PLANNING_READ_ONLY_OPERATIONS``).
PLANNING_READ_ONLY_OPERATIONS: Final[tuple[PlanningOperationName, ...]] = ("get_planning_status",)

#: The 9 synthetic / audit-only operations — they map to ``None`` in
#: :data:`OPERATIONS` (session-types ``PLANNING_SYNTHETIC_OPERATIONS``).
PLANNING_SYNTHETIC_OPERATIONS: Final[tuple[PlanningOperationName, ...]] = (
    "start_planning_session",
    "complete_planning_session",
    "phase_complete",
    "phase_advance",
    "phase_rollback",
    "human_approve",
    "human_reject_with_feedback",
    "human_reject_no_feedback",
    "session_closed",
)


# --------------------------------------------------------------------------- #
# Operation definition records                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GuardSpec:
    """A pre-condition guard that must pass before an operation is accepted."""

    type: GuardType
    description: str


@dataclass(frozen=True)
class OperationDef:
    """Static metadata for a single (non-synthetic) planning operation."""

    name: PlanningOperationName
    kind: OperationKind
    #: The "home" phase where this operation is most expected. ``'any'`` means
    #: the operation is valid in all phases (e.g. ``get_planning_status``).
    native_phase: NativePhase
    #: Phases in which this operation is explicitly forbidden (cannot be called
    #: at all, even as a late operation).
    forbidden_phases: tuple[PlanningPhase, ...]
    #: Whether more than one actor may call this operation concurrently.
    multi_actor: bool
    description: str
    #: Maximum number of items that may be mutated in a single call (``None`` =
    #: uncapped).
    max_batch: int | None = None
    #: Pre-condition guards that must pass before the operation is accepted.
    guards: tuple[GuardSpec, ...] = ()
    #: A CONTIGUOUS band of phases in which the op is native (no rollback), beyond the
    #: single ``native_phase``. When set, ``native_phase`` is the earliest member (the
    #: rollback anchor). Empty = the op is native only in its single ``native_phase``.
    native_phases: tuple[PlanningPhase, ...] = ()


# --------------------------------------------------------------------------- #
# Phase-range helpers used to build forbidden_phases lists                     #
# --------------------------------------------------------------------------- #

_ALL_PHASES: Final[tuple[PlanningPhase, ...]] = (
    PlanningPhase.PLANNING_SPEC,
    PlanningPhase.EPIC_DECOMPOSITION,
    PlanningPhase.EPIC_EXPANSION,
    PlanningPhase.TICKET_DECOMPOSITION,
    PlanningPhase.TICKET_EXPANSION,
    PlanningPhase.CROSS_VALIDATION,
    PlanningPhase.PLANNED,
)


def _phases_before(end: PlanningPhase) -> tuple[PlanningPhase, ...]:
    """Phases from the start up to (and excluding) ``end``.

    ``index(end) <= 0`` yields ``()``.
    """

    try:
        idx = _ALL_PHASES.index(end)
    except ValueError:
        return ()
    return _ALL_PHASES[:idx] if idx > 0 else ()


# --------------------------------------------------------------------------- #
# The registry: 17 operations (16 mutating + 1 read-only) + 9 synthetic nulls  #
# --------------------------------------------------------------------------- #

OPERATIONS: Final[dict[PlanningOperationName, OperationDef | None]] = {
    # 1. update_spec — native: planning_spec; forbidden in all later phases.
    "update_spec": OperationDef(
        name="update_spec",
        kind="mutating",
        native_phase=PlanningPhase.PLANNING_SPEC,
        forbidden_phases=(
            PlanningPhase.EPIC_DECOMPOSITION,
            PlanningPhase.EPIC_EXPANSION,
            PlanningPhase.TICKET_DECOMPOSITION,
            PlanningPhase.TICKET_EXPANSION,
            PlanningPhase.CROSS_VALIDATION,
            PlanningPhase.PLANNED,
        ),
        multi_actor=False,
        description="Update the specification document fields during the planning_spec phase.",
    ),
    # 2. create_epic — native: epic_decomposition; forbidden only in planning_spec.
    "create_epic": OperationDef(
        name="create_epic",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_DECOMPOSITION,
        forbidden_phases=(PlanningPhase.PLANNING_SPEC, PlanningPhase.PLANNED),
        multi_actor=False,
        description=(
            "Create a new epic during the epic_decomposition phase "
            "(also allowed in later phases up to cross_validation)."
        ),
    ),
    # 3. update_epic — native: epic_expansion; forbidden in planning_spec, epic_decomposition.
    "update_epic": OperationDef(
        name="update_epic",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_EXPANSION,
        forbidden_phases=(
            PlanningPhase.PLANNING_SPEC,
            PlanningPhase.EPIC_DECOMPOSITION,
            PlanningPhase.PLANNED,
        ),
        multi_actor=False,
        description="Update epic fields during the epic_expansion phase.",
    ),
    # 4. delete_epic — native: epic_decomposition; forbidden only in planning_spec; guard: minCount.
    "delete_epic": OperationDef(
        name="delete_epic",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_DECOMPOSITION,
        forbidden_phases=(PlanningPhase.PLANNING_SPEC, PlanningPhase.PLANNED),
        multi_actor=False,
        description="Delete an epic (guarded: at least one epic must remain).",
        guards=(
            GuardSpec(
                type="minCount",
                description="At least one epic must remain after deletion.",
            ),
        ),
    ),
    # 5. create_ticket — native: ticket_decomposition;
    #    forbidden in planning_spec, epic_decomposition, epic_expansion.
    "create_ticket": OperationDef(
        name="create_ticket",
        kind="mutating",
        native_phase=PlanningPhase.TICKET_DECOMPOSITION,
        forbidden_phases=(
            PlanningPhase.PLANNING_SPEC,
            PlanningPhase.EPIC_DECOMPOSITION,
            PlanningPhase.EPIC_EXPANSION,
            PlanningPhase.PLANNED,
        ),
        multi_actor=False,
        description="Create a new ticket during the ticket_decomposition phase.",
    ),
    # 6. update_ticket — native: ticket_expansion;
    #    forbidden in planning_spec through ticket_decomposition.
    "update_ticket": OperationDef(
        name="update_ticket",
        kind="mutating",
        native_phase=PlanningPhase.TICKET_EXPANSION,
        forbidden_phases=(
            PlanningPhase.PLANNING_SPEC,
            PlanningPhase.EPIC_DECOMPOSITION,
            PlanningPhase.EPIC_EXPANSION,
            PlanningPhase.TICKET_DECOMPOSITION,
            PlanningPhase.PLANNED,
        ),
        multi_actor=False,
        description="Update ticket fields during the ticket_expansion phase.",
    ),
    # 7. delete_ticket — native: ticket_decomposition;
    #    forbidden in planning_spec, epic_decomposition, epic_expansion; guard: minCount.
    "delete_ticket": OperationDef(
        name="delete_ticket",
        kind="mutating",
        native_phase=PlanningPhase.TICKET_DECOMPOSITION,
        forbidden_phases=(
            PlanningPhase.PLANNING_SPEC,
            PlanningPhase.EPIC_DECOMPOSITION,
            PlanningPhase.EPIC_EXPANSION,
            PlanningPhase.PLANNED,
        ),
        multi_actor=False,
        description="Delete a ticket (guarded: at least one ticket must remain).",
        guards=(
            GuardSpec(
                type="minCount",
                description="At least one ticket must remain after deletion.",
            ),
        ),
    ),
    # 8. create_blueprint — native: epic_decomposition; forbidden in planning_spec.
    #    NO guard: the blueprint:epic ratio is a delete-side hard-deny only.
    "create_blueprint": OperationDef(
        name="create_blueprint",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_DECOMPOSITION,
        forbidden_phases=(PlanningPhase.PLANNING_SPEC, PlanningPhase.PLANNED),
        multi_actor=False,
        description="Create a blueprint during the epic_decomposition phase.",
    ),
    # 9. update_blueprint — native: epic_decomposition; forbidden in planning_spec.
    "update_blueprint": OperationDef(
        name="update_blueprint",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_DECOMPOSITION,
        forbidden_phases=(PlanningPhase.PLANNING_SPEC, PlanningPhase.PLANNED),
        multi_actor=False,
        description="Update blueprint fields during the epic_decomposition phase.",
    ),
    # 10. delete_blueprint — native: epic_decomposition; forbidden in planning_spec;
    #     guard: blueprintEpicRatio.
    "delete_blueprint": OperationDef(
        name="delete_blueprint",
        kind="mutating",
        native_phase=PlanningPhase.EPIC_DECOMPOSITION,
        forbidden_phases=(PlanningPhase.PLANNING_SPEC, PlanningPhase.PLANNED),
        multi_actor=False,
        description="Delete a blueprint (guarded: must respect blueprint-to-epic ratio).",
        guards=(
            GuardSpec(
                type="blueprintEpicRatio",
                description=(
                    "Blueprint count must satisfy the allowed ratio relative to "
                    "epics after deletion."
                ),
            ),
        ),
    ),
    # 11. link_blueprint_to_tickets — native across ticket_decomposition → cross_validation
    #     (the verb is the sole blueprint-link writer; cross_validation settles coverage).
    #     Forbidden in every phase before ticket_decomposition; native_phase is the earliest
    #     band member (the rollback anchor).
    "link_blueprint_to_tickets": OperationDef(
        name="link_blueprint_to_tickets",
        kind="mutating",
        native_phase=PlanningPhase.TICKET_DECOMPOSITION,
        native_phases=(
            PlanningPhase.TICKET_DECOMPOSITION,
            PlanningPhase.TICKET_EXPANSION,
            PlanningPhase.CROSS_VALIDATION,
        ),
        forbidden_phases=(*_phases_before(PlanningPhase.TICKET_DECOMPOSITION), PlanningPhase.PLANNED),
        multi_actor=False,
        description="Link a blueprint to one or more tickets from ticket_decomposition onward.",
    ),
    # 12. unlink_blueprint_to_tickets — native across ticket_decomposition → cross_validation.
    "unlink_blueprint_to_tickets": OperationDef(
        name="unlink_blueprint_to_tickets",
        kind="mutating",
        native_phase=PlanningPhase.TICKET_DECOMPOSITION,
        native_phases=(
            PlanningPhase.TICKET_DECOMPOSITION,
            PlanningPhase.TICKET_EXPANSION,
            PlanningPhase.CROSS_VALIDATION,
        ),
        forbidden_phases=(*_phases_before(PlanningPhase.TICKET_DECOMPOSITION), PlanningPhase.PLANNED),
        multi_actor=False,
        description="Unlink a blueprint from one or more tickets from ticket_decomposition onward.",
    ),
    # 13. create_dependencies — native: cross_validation; forbidden in all phases before it;
    #     max_batch = 5000 (not 100).
    "create_dependencies": OperationDef(
        name="create_dependencies",
        kind="mutating",
        native_phase=PlanningPhase.CROSS_VALIDATION,
        forbidden_phases=(*_phases_before(PlanningPhase.CROSS_VALIDATION), PlanningPhase.PLANNED),
        multi_actor=False,
        description=(
            "Create dependency links between tickets during cross_validation "
            "(max 5000 per call; deduped + cycle-checked before write)."
        ),
        max_batch=5000,
    ),
    # 14. delete_dependencies — native: cross_validation; forbidden in all phases before it.
    "delete_dependencies": OperationDef(
        name="delete_dependencies",
        kind="mutating",
        native_phase=PlanningPhase.CROSS_VALIDATION,
        forbidden_phases=(*_phases_before(PlanningPhase.CROSS_VALIDATION), PlanningPhase.PLANNED),
        multi_actor=False,
        description="Delete dependency links between tickets during cross_validation.",
    ),
    # 15/16. justify / unjustify — the paired N/A-declaration ops. STRUCTURAL-NEUTRAL:
    #     they change no structural set (no files/deps/bodies), only a
    #     {value:'N/A',reason} declaration, so like get_planning_status they are native in
    #     EVERY planning phase and NEVER roll back (native_phase='any'). Valid from
    #     planning_spec through cross_validation; forbidden only once planned.
    "justify": OperationDef(
        name="justify",
        kind="mutating",
        native_phase="any",
        forbidden_phases=(PlanningPhase.PLANNED,),
        multi_actor=False,
        description=(
            "Declare a field N/A with a reason (writes the canonical fieldDeclarations[scope] "
            "key); structural-neutral, native in every planning phase, never rolls back."
        ),
    ),
    "unjustify": OperationDef(
        name="unjustify",
        kind="mutating",
        native_phase="any",
        forbidden_phases=(PlanningPhase.PLANNED,),
        multi_actor=False,
        description=(
            "Remove an N/A declaration (clears the canonical fieldDeclarations[scope] key); "
            "structural-neutral, native in every planning phase, never rolls back."
        ),
    ),
    # 17. get_planning_status — any phase, always native, never forbidden.
    "get_planning_status": OperationDef(
        name="get_planning_status",
        kind="read-only",
        native_phase="any",
        forbidden_phases=(),
        multi_actor=True,
        description=(
            "Read-only-ish poll/resume verb; valid in every phase. "
            "Never re-runs the validator."
        ),
    ),
    # Synthetic / audit-only operations — no callable definition.
    "start_planning_session": None,
    "complete_planning_session": None,
    "phase_complete": None,
    "phase_advance": None,
    "phase_rollback": None,
    "human_approve": None,
    "human_reject_with_feedback": None,
    "human_reject_no_feedback": None,
    "session_closed": None,
}


def _assert_non_synthetic_ops_present() -> None:
    """Exhaustivity check: every mutating/read-only op has a non-null def.

    Guards against a key silently dropping out of :data:`OPERATIONS`.
    """

    for op in (*PLANNING_MUTATING_OPERATIONS, *PLANNING_READ_ONLY_OPERATIONS):
        if OPERATIONS.get(op) is None:
            raise RuntimeError(f"OPERATIONS record is missing a def for non-synthetic op: {op}")


_assert_non_synthetic_ops_present()


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


def classify_operation_call(
    op: PlanningOperationName,
    current_phase: PlanningPhase,
) -> OperationCallClass:
    """Classify a call to ``op`` in ``current_phase``.

    Returns:
        * ``'forbidden'`` — the operation must be rejected in this phase.
        * ``'native'`` — ``current_phase`` is the home phase (or op is ``'any'``).
        * ``'late'`` — allowed, but the caller is working past the native phase.

    Raises:
        ValueError: if ``op`` is a synthetic / audit-only operation (no def).
    """

    spec = OPERATIONS.get(op)
    if spec is None:
        raise ValueError(f"No operation def for audit-only op: {op}")

    if current_phase in spec.forbidden_phases:
        return "forbidden"
    # An op native across a CONTIGUOUS band (native_phases) is native in every member —
    # no rollback (e.g. blueprint-link across ticket_decomposition → cross_validation).
    if current_phase in spec.native_phases:
        return "native"
    if spec.native_phase == "any" or spec.native_phase == current_phase:
        return "native"
    # Not forbidden and not the native phase: it must be a late call.
    return "late"


def get_operation_def(op: PlanningOperationName) -> OperationDef | None:
    """Return the :class:`OperationDef` for ``op``, or ``None`` if synthetic."""

    return OPERATIONS.get(op)
