"""Handover verb contracts — the three payloads + the ``HandoverOutcome`` envelope.

The handover surface. The three handover verbs (``approve`` / ``reject`` /
``reject_with_feedback``) are **separate entrypoints** — they are NOT part of the
agent ``dispatch.handle`` union; the CLI/MCP calls them directly and persists the
returned ``write_plan``.

Each verb is a pure ``(payload, ports) -> HandoverOutcome``:

* :class:`HandoverResult` (``ok=True``) carries the post-mutation ``new_status`` /
  ``new_phase``, the composed agent :class:`PlanningAgentResponse` (the human runs
  these via the CLI/MCP and wants guidance prose alongside the outcome), and the
  :class:`WritePlan` the entrypoint commits.
* :class:`HandoverError` (``ok=False``) is the typed pre-condition failure envelope
  (``SESSION_NOT_FOUND`` / ``HANDOVER_NOT_PENDING`` / ``INVALID_FEEDBACK`` /
  ``HANDOVER_GATE_FAILING``). It carries no write plan — nothing is persisted.

The union is closed: a verb returns exactly one of the two; ``isinstance`` (or the
``ok`` discriminator) tells them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from specsmither.domain.enums import PlanningPhase, PlanningSessionStatus
from specsmither.lifecycle.guidance.types import PlanningAgentResponse
from specsmither.lifecycle.write_plan_types import WritePlan

__all__ = [
    "ApproveHandoverPayload",
    "HandoverError",
    "HandoverErrorCode",
    "HandoverOutcome",
    "HandoverResult",
    "RejectHandoverPayload",
    "RejectHandoverWithFeedbackPayload",
]

#: The closed set of handover pre-condition failure codes (``HandoverErrorCode``).
#: ``HANDOVER_GATE_FAILING`` is the gate-on-pass approval precondition.
HandoverErrorCode = Literal[
    "SESSION_NOT_FOUND",
    "HANDOVER_NOT_PENDING",
    "INVALID_FEEDBACK",
    "HANDOVER_GATE_FAILING",
]


@dataclass(frozen=True)
class ApproveHandoverPayload:
    """``approve_handover`` input (``ApproveHandoverPayload``).

    ``user_id`` is optional locally (single local user); it is stamped onto the audit
    rows' ``performed_by_user_id`` when present.
    """

    session_id: str
    user_id: str | None = None


@dataclass(frozen=True)
class RejectHandoverPayload:
    """``reject_handover`` (no-feedback) input (``RejectHandoverPayload``)."""

    session_id: str
    user_id: str | None = None


@dataclass(frozen=True)
class RejectHandoverWithFeedbackPayload:
    """``reject_handover_with_feedback`` input (``RejectHandoverWithFeedbackPayload``).

    ``feedback`` must be a non-empty string (else ``INVALID_FEEDBACK``); it is stashed
    on the session for one-shot delivery to the agent on its next status poll.
    """

    session_id: str
    feedback: str
    user_id: str | None = None


@dataclass(frozen=True)
class HandoverResult:
    """A successful handover (``HandoverResult``).

    ``new_status`` / ``new_phase`` are the POST-mutation session state (``'closed'`` at
    the terminal approve, ``'active'`` otherwise). ``response`` is the composed
    agent-facing guidance; ``write_plan`` is the atomic mutation the entrypoint commits
    in one transaction.
    """

    session_id: str
    new_status: PlanningSessionStatus
    new_phase: PlanningPhase
    response: PlanningAgentResponse
    write_plan: WritePlan
    ok: Literal[True] = True


@dataclass(frozen=True)
class HandoverError:
    """A typed handover pre-condition failure (``HandoverError``).

    Carries a stable machine ``code`` + a rendered English ``message`` and **no** write
    plan — the failing verb persists nothing.
    """

    code: HandoverErrorCode
    message: str
    ok: Literal[False] = False


#: The closed union every handover verb returns.
HandoverOutcome = HandoverResult | HandoverError
