"""The lifecycle dispatch facade (work item #10) — ported from ``index.ts`` (A1 §0).

:func:`create_lifecycle` mirrors the TS ``createLifecycle(ports)``: it returns a
:class:`Lifecycle` whose :meth:`Lifecycle.handle` does exactly two things — route the
event to its (pure) verb, then, if the verb built a :class:`WritePlan` and a
``persist_write_plan`` port is wired, persist it. Both happen inside the caller's
single transaction (the verb's reads + the plan's writes share one ``Session`` →
one ``BEGIN IMMEDIATE``).

The four agent-facing verbs (``start`` / ``action`` / ``complete`` / ``inspect``) are
the dispatch union; the three handover verbs are deliberately NOT here (they are
webapp/CLI-called entrypoints built separately, mirroring the TS design where
``approveHandover`` / ``rejectHandover*`` are not part of ``handle``).

:func:`run_verb` is the one-call convenience for callers that own a
``sessionmaker``: it opens one ``session_factory.begin()`` transaction, binds the
COUPLED ports over that session via
:func:`~specsmither.adapters.lifecycle_ports.make_lifecycle_ports`, and runs the verb
— so the verb's reads, the WritePlan persist, and the in-transaction recompute all
commit (or roll back) atomically. ``clock`` / ``id_generator`` are injectable for
determinism; ``validator`` swaps the crucible adapter for a stub in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from specsmither.adapters.lifecycle_ports import make_lifecycle_ports
from specsmither.lifecycle.verbs.action import action_planning_session
from specsmither.lifecycle.verbs.complete import complete_planning_session
from specsmither.lifecycle.verbs.inspect import inspect_planning_session
from specsmither.lifecycle.verbs.start import start_planning_session
from specsmither.lifecycle.verbs.support import VerbResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.lifecycle.guidance.types import PlanningAgentResponse
    from specsmither.lifecycle.ports import Clock, IdGenerator, LifecyclePorts, Validator

__all__ = [
    "Lifecycle",
    "LifecycleEvent",
    "VerbName",
    "create_lifecycle",
    "run_verb",
]

#: The four agent-facing verb names the dispatch union routes.
VerbName = Literal["start", "action", "complete", "inspect"]


@dataclass(frozen=True)
class LifecycleEvent:
    """A dispatchable lifecycle event: ``{verb, payload}`` (the TS ``PlanningLifecycleEvent``).

    ``verb`` selects the agent-facing verb; ``payload`` is that verb's wire payload
    (``{specId,…}`` for ``start``; ``{sessionId, operation, payload?, actor?,…}`` for
    ``action``; ``{sessionId,…}`` for ``complete`` / ``inspect``).
    """

    verb: VerbName
    payload: Mapping[str, Any]


class Lifecycle:
    """The dispatch object ``create_lifecycle`` returns (the TS ``Lifecycle``).

    Holds the bound :class:`LifecyclePorts`; :meth:`handle` routes + persists.
    """

    def __init__(self, ports: LifecyclePorts) -> None:
        self._ports = ports

    def handle(self, event: LifecycleEvent) -> PlanningAgentResponse:
        """Route ``event`` to its verb, persist its WritePlan, return the response.

        The verb BUILDS the plan; this persists it via ``ports.persist_write_plan``
        (a denial also carries an audit-only plan, so persist whenever one exists and
        the port is wired). When no ``persist_write_plan`` is wired (the in-memory/dev
        path) the plan is built but not committed.
        """
        result = _dispatch(event, self._ports)
        if self._ports.persist_write_plan is not None and result.write_plan is not None:
            self._ports.persist_write_plan(result.write_plan)
        return result.response


def create_lifecycle(ports: LifecyclePorts) -> Lifecycle:
    """Wrap ``ports`` in a :class:`Lifecycle` dispatch facade (``createLifecycle``)."""
    return Lifecycle(ports)


def _dispatch(event: LifecycleEvent, ports: LifecyclePorts) -> VerbResult:
    """Route an agent-facing event to its verb (``dispatch``)."""
    if event.verb == "start":
        return start_planning_session(event.payload, ports)
    if event.verb == "action":
        return action_planning_session(event.payload, ports)
    if event.verb == "complete":
        return complete_planning_session(event.payload, ports)
    if event.verb == "inspect":
        return inspect_planning_session(event.payload, ports)
    raise ValueError(f"Unknown lifecycle verb: {event.verb!r}")


def run_verb(
    session_factory: sessionmaker[Session],
    event: LifecycleEvent,
    *,
    clock: Clock | None = None,
    id_generator: IdGenerator | None = None,
    validator: Validator | None = None,
) -> PlanningAgentResponse:
    """Open one transaction, bind the ports, and run ``event`` end-to-end.

    The verb's reads, the WritePlan persist, and the recompute worklist all run inside
    the single ``session_factory.begin()`` transaction (one ``BEGIN IMMEDIATE``), then
    commit together. ``validator`` (when given) replaces the crucible adapter — the
    test seam for driving the gate deterministically.
    """
    with session_factory.begin() as session:
        ports = make_lifecycle_ports(session, clock=clock, id_generator=id_generator)
        if validator is not None:
            ports = replace(ports, validator=validator)
        return create_lifecycle(ports).handle(event)
