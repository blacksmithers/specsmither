"""The runtime entrypoint that wires a pure handover verb to the SQLite seam.

The three handover verbs (:mod:`specsmither.lifecycle.verbs`) are pure
``(payload, ports) -> HandoverOutcome`` functions — they read via the ports and *build*
a :class:`~specsmither.adapters.write_plan_executor.WritePlan`, but never persist.
:func:`run_handover` is the thin transactional shell the CLI / MCP (L5 / L6) calls: it
opens one ``BEGIN IMMEDIATE`` (architecture invariant 3), binds the coupled
:class:`~specsmither.lifecycle.ports.LifecyclePorts` over that session via
:func:`~specsmither.adapters.lifecycle_ports.make_lifecycle_ports`, runs the verb, and —
on success — commits the verb's write plan through ``ports.persist_write_plan`` inside the
same transaction. A :class:`~specsmither.lifecycle.verbs.types.HandoverError` carries no
write plan, so nothing is persisted and the (read-only) transaction commits as a no-op.

This is the handover analogue of the agent-verb ``run_verb`` entrypoint (L5). The actor
for every handover is ``human`` — baked into the verbs themselves, so this shell stays
verb-agnostic: it takes any of the three handover verbs as ``verb`` plus its matching
typed payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from specsmither.adapters.lifecycle_ports import make_lifecycle_ports
from specsmither.lifecycle.verbs.types import HandoverOutcome, HandoverResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.lifecycle.ports import Clock, IdGenerator, LifecyclePorts

__all__ = ["run_handover"]

_P = TypeVar("_P")


def run_handover(
    session_factory: sessionmaker[Session],
    verb: Callable[[_P, LifecyclePorts], HandoverOutcome],
    payload: _P,
    *,
    clock: Clock | None = None,
    id_generator: IdGenerator | None = None,
) -> HandoverOutcome:
    """Open a transaction, run ``verb(payload, ports)``, persist its plan, return the outcome.

    ``verb`` is one of :func:`~specsmither.lifecycle.verbs.approve.approve_handover` /
    :func:`~specsmither.lifecycle.verbs.reject.reject_handover` /
    :func:`~specsmither.lifecycle.verbs.reject_with_feedback.reject_handover_with_feedback`,
    and ``payload`` is its matching dataclass (the ``_P`` type var binds the two together).
    ``clock`` / ``id_generator`` are injectable for determinism (defaulting to UTC-now and
    the monotonic ULID minter). The whole plan — entity mutation, session update, audit
    rows, and the in-transaction recompute — commits atomically, or rolls back on error.
    """

    with session_factory.begin() as session:
        ports = make_lifecycle_ports(session, clock=clock, id_generator=id_generator)
        outcome = verb(payload, ports)
        if isinstance(outcome, HandoverResult) and ports.persist_write_plan is not None:
            ports.persist_write_plan(outcome.write_plan)
        return outcome
