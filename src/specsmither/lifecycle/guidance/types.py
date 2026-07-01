"""The verb response shape — :class:`PlanningAgentResponse` + its sub-blocks.

A faithful-but-widened port of the TS ``PlanningAgentResponse`` (session-types
``lifecycle-contract.ts:22-31``). The TS shape is deliberately flat — eight scalar
fields plus a single ``guidance`` prose string — because the AppSync resolver echoes
it verbatim into the ``McpLifecycleEnvelope`` (``api-types/mcp/lifecycle-envelope.ts``)
and the client reads the typed score / gate / phase / status fields directly, never
parsing the prose.

This port keeps every TS scalar (renamed snake-case: ``sessionId`` → ``session_id``,
``planningPhase`` → ``phase``, ``planningStatus`` → ``status``, ``scoreLocal`` →
``score``, ``scoreGlobal`` → ``score_global``, ``gatePassed`` →
:attr:`PlanningAgentResponse.gate_passed`) and adds the structured side-blocks the L5
MCP layer will surface alongside the prose:

* ``next_entities`` — the expansion-phase "what to work on next" list, already capped
  by ``guidance.maxNextEntitiesToShow`` at compose time.
* ``recommended_moves`` — finding-derived ``{operation, rationale}`` hints (each maps a
  validator finding's path to the fix verb via the audit operation-mapper).
* ``findings`` — a summarized, severity-tagged view of the validator findings.

``response_kind`` is the StubResponse-friendly discriminator: every real lifecycle
response carries ``"planning_agent_response"`` so the dispatch facade (L5) can branch a
genuine verb result from an error/stub envelope without inspecting other fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from specsmither.domain.enums import GuidanceVariant, PlanningPhase, PlanningSessionStatus

__all__ = [
    "FindingSummary",
    "GateResult",
    "NextEntity",
    "Outcome",
    "PlanningAgentResponse",
    "RecommendedMove",
]

#: The verb outcome axis — self-discriminates success vs. denial (the envelope ships a
#: denial as HTTP 200 with ``outcome == 'denied'``; there is no separate error code).
Outcome = Literal["success", "denied"]

#: The binary gate verdict surfaced to the agent (``None`` = not yet scored this phase).
GateResult = Literal["pass", "fail"]


@dataclass(frozen=True)
class RecommendedMove:
    """One finding-derived next-step hint (``ProcessGuidance.recommendedMoves[]``).

    ``operation`` is the :data:`PlanningOperationName` the agent should call to clear
    the finding (resolved from the finding ``path`` by the audit operation-mapper);
    ``rationale`` is the finding's English message, surfaced verbatim.
    """

    operation: str
    rationale: str


@dataclass(frozen=True)
class NextEntity:
    """One entry of the expansion-phase "next to work on" list (capped at compose time).

    ``entity_type`` is ``'spec'`` / ``'epic'`` / ``'ticket'``; ``score`` is the entity's
    current per-entity score when known; ``hint`` is the first review hint grouped for
    that entity (a validator finding message), or ``None``.
    """

    entity_id: str
    entity_type: str
    score: float | None = None
    hint: str | None = None


@dataclass(frozen=True)
class FindingSummary:
    """A summarized validator finding (the ``ProcessGuidance.findings`` block, flattened).

    Carries only what the agent needs to act: the coarse ``category``, the English
    ``message``, the ``severity`` (``'finding'`` advisory vs. ``'denial'`` blocking), and
    the optional ``entity_id`` / ``path`` locators.
    """

    category: str
    message: str
    severity: Literal["finding", "denial"]
    entity_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class PlanningAgentResponse:
    """The response every planning verb returns, on BOTH success and denial branches.

    The scalar head mirrors the TS ``PlanningAgentResponse`` (so the L5 envelope can echo
    it field-for-field); the list tail (``next_entities`` / ``recommended_moves`` /
    ``findings``) carries the structured guidance the MCP client renders next to the
    prose. ``score`` is the spec-local score; ``score_global`` is the project-wide score
    when available. ``gate_result`` is the surfaced binary verdict — read
    :attr:`gate_passed` for the TS ``gatePassed`` boolean.
    """

    outcome: Outcome
    session_id: str
    spec_id: str
    phase: PlanningPhase
    status: PlanningSessionStatus
    variant: GuidanceVariant
    guidance: str
    gate_result: GateResult | None = None
    score: float | None = None
    score_global: float | None = None
    next_entities: list[NextEntity] = field(default_factory=list)
    recommended_moves: list[RecommendedMove] = field(default_factory=list)
    findings: list[FindingSummary] = field(default_factory=list)
    findings_summary: str | None = None
    #: StubResponse-friendly discriminator (constant for every real verb response).
    response_kind: Literal["planning_agent_response"] = "planning_agent_response"

    @property
    def gate_passed(self) -> bool | None:
        """The TS ``gatePassed`` boolean: ``None`` when the gate has not been evaluated."""

        if self.gate_result is None:
            return None
        return self.gate_result == "pass"
