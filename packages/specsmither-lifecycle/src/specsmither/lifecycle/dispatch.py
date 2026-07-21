"""The lifecycle dispatch facade.

:func:`create_lifecycle` returns a
:class:`Lifecycle` whose :meth:`Lifecycle.handle` does exactly two things — route the
event to its (pure) verb, then, if the verb built a :class:`WritePlan` and a
``persist_write_plan`` port is wired, persist it. Both happen inside the caller's
single transaction (the verb's reads + the plan's writes share one ``Session`` →
one ``BEGIN IMMEDIATE``).

The three agent-facing verbs (``start`` / ``action`` / ``complete``) are the dispatch
union — the read-only status poll is the ``get_planning_status`` operation of ``action``,
not a separate verb; the three handover verbs are deliberately NOT here (they are
webapp/CLI-called entrypoints built separately, where
``approveHandover`` / ``rejectHandover*`` are not part of ``handle``).

This module is PURE (no sqlalchemy): it dispatches to the verbs and persists via the
injected ``persist_write_plan`` port. The transactional one-call convenience that binds
the COUPLED SQLite ports over a ``sessionmaker`` — ``run_verb`` — lives on the product
side in :func:`specsmither.adapters.lifecycle_runner.run_verb`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from specsmither.lifecycle.verbs.action import action_planning_session
from specsmither.lifecycle.verbs.complete import complete_planning_session
from specsmither.lifecycle.verbs.start import start_planning_session
from specsmither.lifecycle.verbs.support import VerbResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from specsmither.lifecycle.guidance.types import PlanningAgentResponse
    from specsmither.lifecycle.ports import LifecyclePorts

__all__ = [
    "Lifecycle",
    "LifecycleEvent",
    "VerbName",
    "create_lifecycle",
]

#: The three agent-facing verb names the dispatch union routes.
VerbName = Literal["start", "action", "complete"]


@dataclass(frozen=True)
class LifecycleEvent:
    """A dispatchable lifecycle event: ``{verb, payload}``.

    ``verb`` selects the agent-facing verb; ``payload`` is that verb's wire payload
    (``{specId,…}`` for ``start``; ``{sessionId, operation, payload?, actor?,…}`` for
    ``action``; ``{sessionId,…}`` for ``complete``). The read-only status poll is the
    ``get_planning_status`` operation of ``action``, not a separate verb.
    """

    verb: VerbName
    payload: Mapping[str, Any]


class Lifecycle:
    """The dispatch object ``create_lifecycle`` returns.

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
    raise ValueError(f"Unknown lifecycle verb: {event.verb!r}")
