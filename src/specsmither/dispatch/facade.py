"""The M7 dispatch facade (work item #12) — the single MCP routing seam.

A faithful port of ``core/tools.ts`` + ``core/handlers/*`` (recon A8 §4): the **only**
module allowed to import both the planning :mod:`~specsmither.lifecycle` state machine
and the :mod:`~specsmither.operations` CRUD/query primitives (the TS M7 §2.1b façade
rule). The MCP server (L6) talks ONLY to this façade and never reaches into lifecycle
or operations directly, so the composition / fan-out lives here.

:class:`Dispatcher` routes a tool name + a wire ``arguments`` mapping to exactly ONE of
the three content shapes the :mod:`~specsmither.dispatch.envelopes` layer defines:

#. **lifecycle** (``{kind: 'lifecycle', agent_response}``) — the three planning verbs
   (``start`` / ``action`` / ``complete``) AND the three handover verbs forward verbatim
   to the lifecycle; a denial rides the same envelope (it self-discriminates via
   ``agent_response.outcome == 'denied'`` — it is never an exception).
#. **bare success payload** — the query / mutation composers return the raw, un-enveloped
   operation result (a JSON-able dict, no ``kind`` discriminator).
#. **standard_error** (``{kind: 'standard_error', code, message, guidance, context}``) —
   any raised exception (a domain :class:`~specsmither.operations.errors.CrudError`, a
   handover :class:`~specsmither.lifecycle.verbs.types.HandoverError`, or an unknown
   tool) is mapped to CONTENT here, never raised to the protocol.

The four work-session verbs (``start`` / ``action`` / ``complete`` / ``reset``) are
frozen-zone in 0.1.0: they resolve to a :func:`~specsmither.dispatch.envelopes.stub_response`
``{status: 'in_development', planned_for: '0.2.0'}`` instead of throwing.

The engine is SYNC: every primitive runs inside one ``session_factory`` transaction
(the lifecycle verbs via :func:`~specsmither.lifecycle.dispatch.run_verb`, the handover
verbs via an inline ports-bound transaction mirroring it, the operations via their own
``session_factory.begin()``). Only the L6 MCP server is async.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from specsmither.adapters.lifecycle_ports import make_lifecycle_ports
from specsmither.dispatch.envelopes import (
    WORK_VERBS,
    ResponseDetail,
    lifecycle_envelope,
    normalise_response_detail,
    standard_error_envelope,
    stub_response,
    success_payload,
)
from specsmither.dispatch.error_guidance import (
    ErrorGuidance,
    normalise_error,
    unknown_tool_guidance,
)
from specsmither.lifecycle.dispatch import LifecycleEvent, VerbName, run_verb
from specsmither.lifecycle.verbs.approve import approve_handover
from specsmither.lifecycle.verbs.reject import reject_handover
from specsmither.lifecycle.verbs.reject_with_feedback import reject_handover_with_feedback
from specsmither.lifecycle.verbs.types import (
    ApproveHandoverPayload,
    HandoverError,
    HandoverOutcome,
    HandoverResult,
    RejectHandoverPayload,
    RejectHandoverWithFeedbackPayload,
)
from specsmither.operations.errors import (
    CrudError,
    NotFoundError,
    PreconditionFailedError,
    ValidationFailedError,
    to_crud_error,
)
from specsmither.operations.pull_request import link_pull_request
from specsmither.operations.queries import (
    get_blocked_tickets,
    get_critical_path,
    get_dependency_tree,
    get_epic,
    get_next_actionable_tickets,
    get_project,
    get_specification,
    get_ticket,
    list_epics,
    list_projects,
    list_specifications,
    list_tickets,
)
from specsmither.operations.reopen import reopen_specification
from specsmither.operations.reports import (
    active_sessions,
    blockers_report,
    dashboard_report,
    implementation_summary,
    readiness_report,
    time_report,
)
from specsmither.operations.search import search_tickets

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "HANDOVER_TOOL_NAMES",
    "TOOL_NAMES",
    "Dispatcher",
    "make_dispatcher",
]


# --------------------------------------------------------------------------------------
# Tool vocabulary.
# --------------------------------------------------------------------------------------

#: The 18 base MCP tools the façade advertises + routes (the work-session verbs are the
#: four 0.1.0 stubs). Handover verbs are exposed separately (:data:`HANDOVER_TOOL_NAMES`).
TOOL_NAMES: tuple[str, ...] = (
    # Lifecycle — planning (3).
    "start_planning_session",
    "action_planning_session",
    "complete_planning_session",
    # Work-session verbs — frozen-zone 0.1.0 stubs (4).
    "start_work_session",
    "action_work_session",
    "complete_work_session",
    "reset_work_session",
    # Queries (8).
    "get",
    "list",
    "search",
    "get_next_actionable_tickets",
    "get_blocked_tickets",
    "get_report",
    "get_critical_path",
    "get_dependency_tree",
    # Mutations (2).
    "reopen_specification",
    "link_pull_request",
    # Utility (1).
    "feedback",
)

#: The three handover verbs — MCP-callable too, per the 0.1.0 actor model. They forward
#: to the standalone (non-dispatch-union) lifecycle handover runners.
HANDOVER_TOOL_NAMES: tuple[str, ...] = (
    "approve_handover",
    "reject_handover",
    "reject_handover_with_feedback",
)

#: Every routable tool — fed to the unknown-tool "did you mean" guidance.
_ALL_TOOLS: tuple[str, ...] = (*TOOL_NAMES, *HANDOVER_TOOL_NAMES)

#: Planning tool -> the lifecycle dispatch-union verb name.
_PLANNING_VERBS: dict[str, VerbName] = {
    "start_planning_session": "start",
    "action_planning_session": "action",
    "complete_planning_session": "complete",
}

#: The query / mutation tools routed to the operations primitives (bare success payload).
_OPERATION_TOOLS: frozenset[str] = frozenset(
    {
        "get",
        "list",
        "search",
        "get_next_actionable_tickets",
        "get_blocked_tickets",
        "get_report",
        "get_critical_path",
        "get_dependency_tree",
        "reopen_specification",
        "link_pull_request",
    }
)

_FEEDBACK_TOOL = "feedback"

#: The entity kinds the `get` super-tool fans out across.
_GET_TYPES: tuple[str, ...] = ("specification", "epic", "ticket", "project")

#: The entity kinds the `list` super-tool fans out across.
_LIST_TYPES: tuple[str, ...] = ("specifications", "epics", "tickets", "projects")

#: The analytics report kinds `get_report` dispatches (all return dict-shaped records).
_REPORT_KINDS: tuple[str, ...] = (
    "dashboard",
    "implementation",
    "time",
    "blockers",
    "readiness",
    "sessions",
)

#: Wire (camelCase) -> primitive (snake_case) keyword names for the `search` super-tool.
_SEARCH_PARAMS: dict[str, str] = {
    "query": "query",
    "files": "files",
    "tags": "tags",
    "matchAllTags": "match_all_tags",
    "relatedTo": "related_to",
    "status": "status",
    "complexity": "complexity",
    "projectId": "project_id",
    "specificationId": "specification_id",
    "epicId": "epic_id",
    "limit": "limit",
    "offset": "offset",
}


def _utc_now() -> datetime:
    """The default report clock — a timezone-aware UTC instant."""
    return datetime.now(UTC)


def _as_dict(payload: object) -> dict[str, Any]:
    """Coerce a bare success payload to a JSON-able ``dict`` (the wire content contract).

    Every routed operation returns a record / page / report that normalises to a dict;
    the ``{"result": …}`` wrapper is a type-honest safety net for any non-dict payload.
    """
    if isinstance(payload, dict):
        return payload
    return {"result": payload}


def _guidance_to_dict(guidance: ErrorGuidance | None) -> dict[str, object] | None:
    """Flatten an :class:`ErrorGuidance` to the envelope's ``guidance`` dict (or ``None``)."""
    if guidance is None:
        return None
    return {
        "prose": guidance.prose,
        "next_actions": list(guidance.next_actions),
        "related": list(guidance.related),
    }


class Dispatcher:
    """Routes the 18 (+3 handover) MCP tools to lifecycle / operations primitives.

    Constructed with a ``session_factory`` (every primitive runs in one transaction over
    it) and an optional report ``clock`` (injected into the ``get_report`` analytics
    functions for determinism). :meth:`dispatch` is the single entrypoint; it always
    returns a JSON-able dict — one of the three content envelopes — and never raises a
    domain error (those become ``standard_error`` content).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | datetime | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock: Callable[[], datetime] | datetime = clock if clock is not None else _utc_now

    def dispatch(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        response_detail: ResponseDetail = "standard",
    ) -> dict[str, Any]:
        """Route ``tool`` + ``arguments`` to its handler and return content (never raise).

        ``response_detail`` is the verbosity tier (forwarded into the planning payload,
        where the response builder honours it). Any exception a primitive raises is
        mapped to a ``standard_error`` envelope — domain errors are content, not protocol
        errors.
        """
        try:
            if tool in _PLANNING_VERBS:
                return self._dispatch_planning(tool, arguments, response_detail)
            if tool in HANDOVER_TOOL_NAMES:
                return self._dispatch_handover(tool, arguments)
            if tool in WORK_VERBS:
                return stub_response(tool)
            if tool in _OPERATION_TOOLS:
                return _as_dict(success_payload(self._run_operation(tool, arguments)))
            if tool == _FEEDBACK_TOOL:
                return self._dispatch_feedback(arguments)
            return self._error_envelope(*unknown_tool_guidance(tool, _ALL_TOOLS))
        except Exception as exc:
            # A domain error is CONTENT (a standard_error envelope), never raised to the
            # protocol — this single boundary guarantees that invariant for every tool.
            code, message, guidance, context = self._normalise(exc, tool)
            if str(code) == "INTERNAL":
                # An unexpected exception (a bug, not a domain error) — log the traceback to
                # stderr so it is diagnosable instead of vanishing behind an opaque envelope.
                import sys
                import traceback

                print(f"[specsmither] INTERNAL error in tool {tool!r}:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            return self._error_envelope(code, message, guidance, context)

    # ---------------------------------------------------------------------------------- #
    # Lifecycle — planning verbs (forward verbatim; denial rides the same envelope).      #
    # ---------------------------------------------------------------------------------- #

    def _dispatch_planning(
        self, tool: str, arguments: Mapping[str, Any], response_detail: ResponseDetail
    ) -> dict[str, Any]:
        detail = normalise_response_detail(arguments.get("responseDetail", response_detail))
        payload: dict[str, Any] = {**arguments, "responseDetail": detail}
        event = LifecycleEvent(verb=_PLANNING_VERBS[tool], payload=payload)
        response = run_verb(self._session_factory, event)
        return lifecycle_envelope(response)

    # ---------------------------------------------------------------------------------- #
    # Lifecycle — handover runners (standalone verbs; commit the writePlan in-txn).        #
    # ---------------------------------------------------------------------------------- #

    def _dispatch_handover(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        outcome = self._run_handover(tool, arguments)
        if isinstance(outcome, HandoverResult):
            return lifecycle_envelope(outcome.response)
        return self._handover_error_envelope(outcome, tool)

    def _run_handover(self, tool: str, arguments: Mapping[str, Any]) -> HandoverOutcome:
        session_id = _arg(arguments, "sessionId")
        user_id = _arg(arguments, "userId")
        with self._session_factory.begin() as session:
            ports = make_lifecycle_ports(session)
            if tool == "approve_handover":
                outcome: HandoverOutcome = approve_handover(
                    ApproveHandoverPayload(session_id=session_id, user_id=user_id), ports
                )
            elif tool == "reject_handover":
                outcome = reject_handover(
                    RejectHandoverPayload(session_id=session_id, user_id=user_id), ports
                )
            else:
                outcome = reject_handover_with_feedback(
                    RejectHandoverWithFeedbackPayload(
                        session_id=session_id,
                        feedback=arguments.get("feedback") or "",
                        user_id=user_id,
                    ),
                    ports,
                )
            # The successful handover carries an UN-committed writePlan; commit it inside
            # this same transaction (mirrors the TS handover runner's explicit persist).
            if isinstance(outcome, HandoverResult) and ports.persist_write_plan is not None:
                ports.persist_write_plan(outcome.write_plan)
            return outcome

    def _handover_error_envelope(self, error: HandoverError, tool: str) -> dict[str, Any]:
        """Map a typed handover precondition failure to a ``standard_error`` envelope."""
        if error.code == "SESSION_NOT_FOUND":
            exc: CrudError = NotFoundError(
                error.message, context={"entity_type": "planning_session"}
            )
        elif error.code == "INVALID_FEEDBACK":
            exc = ValidationFailedError(error.message, context={"failing_field": "feedback"})
        else:  # HANDOVER_NOT_PENDING / HANDOVER_GATE_FAILING — a bad state precondition.
            exc = PreconditionFailedError(
                error.message, context={"entity_type": "planning_session"}
            )
        return self._error_envelope(*normalise_error(exc, tool=tool))

    # ---------------------------------------------------------------------------------- #
    # Queries / mutations — thin composers over the operations primitives.                #
    # ---------------------------------------------------------------------------------- #

    def _run_operation(self, tool: str, arguments: Mapping[str, Any]) -> object:
        factory = self._session_factory
        if tool == "get":
            return self._get(arguments)
        if tool == "list":
            return self._list(arguments)
        if tool == "search":
            kwargs = {
                snake: arguments[camel]
                for camel, snake in _SEARCH_PARAMS.items()
                if camel in arguments and arguments[camel] is not None
            }
            return search_tickets(factory, **kwargs)
        if tool == "get_next_actionable_tickets":
            limit = arguments.get("limit")
            if limit is None:
                return get_next_actionable_tickets(factory, _arg(arguments, "specificationId"))
            return get_next_actionable_tickets(
                factory, _arg(arguments, "specificationId"), limit=limit
            )
        if tool == "get_blocked_tickets":
            return get_blocked_tickets(factory, _arg(arguments, "specificationId"))
        if tool == "get_critical_path":
            return get_critical_path(factory, _arg(arguments, "specificationId"))
        if tool == "get_dependency_tree":
            return get_dependency_tree(factory, _arg(arguments, "specificationId"))
        if tool == "get_report":
            return self._report(arguments)
        if tool == "reopen_specification":
            return reopen_specification(factory, _arg(arguments, "specificationId"))
        # link_pull_request — the only remaining operation tool.
        return link_pull_request(
            factory,
            _arg(arguments, "specificationId"),
            _arg(arguments, "prNumber"),
            url=arguments.get("url"),
            title=arguments.get("title"),
            state=arguments.get("state"),
        )

    def _get(self, arguments: Mapping[str, Any]) -> object:
        entity_type = arguments.get("type")
        entity_id = _arg(arguments, "id")
        if entity_type == "specification":
            return get_specification(self._session_factory, entity_id)
        if entity_type == "epic":
            return get_epic(self._session_factory, entity_id)
        if entity_type == "ticket":
            return get_ticket(self._session_factory, entity_id)
        if entity_type == "project":
            return get_project(self._session_factory, entity_id)
        raise ValidationFailedError(
            f"get requires 'type' to be one of: {', '.join(_GET_TYPES)}",
            context={"failing_field": "type"},
        )

    def _list(self, arguments: Mapping[str, Any]) -> object:
        entity_type = arguments.get("type")
        factory = self._session_factory
        fields = arguments.get("fields")
        limit = arguments.get("limit")
        offset = arguments.get("offset", 0)
        cursor = arguments.get("cursor")
        if entity_type == "specifications":
            return list_specifications(
                factory, _arg(arguments, "projectId"),
                fields=fields, limit=limit, offset=offset, cursor=cursor,
            )
        if entity_type == "epics":
            return list_epics(
                factory, _arg(arguments, "specificationId"),
                fields=fields, limit=limit, offset=offset, cursor=cursor,
            )
        if entity_type == "tickets":
            return list_tickets(
                factory,
                epic_id=arguments.get("epicId"),
                specification_id=arguments.get("specificationId"),
                status=arguments.get("status"),
                fields=fields, limit=limit, offset=offset, cursor=cursor,
            )
        if entity_type == "projects":
            return list_projects(factory, fields=fields, limit=limit, offset=offset, cursor=cursor)
        raise ValidationFailedError(
            f"list requires 'type' to be one of: {', '.join(_LIST_TYPES)}",
            context={"failing_field": "type"},
        )

    def _report(self, arguments: Mapping[str, Any]) -> object:
        kind = arguments.get("report") or arguments.get("type")
        factory = self._session_factory
        clock = self._clock
        spec_id = self._scope_id(arguments, "specificationId", "specification")
        project_id = self._scope_id(arguments, "projectId", "project")
        epic_id = self._scope_id(arguments, "epicId", "epic")
        if kind == "dashboard":
            return dashboard_report(factory, _require(spec_id, "specificationId"), now=clock)
        if kind == "implementation":
            return implementation_summary(
                factory, now=clock, project_id=project_id, specification_id=spec_id
            )
        if kind == "time":
            return time_report(
                factory, now=clock,
                project_id=project_id, specification_id=spec_id, epic_id=epic_id,
            )
        if kind == "blockers":
            return blockers_report(
                factory, now=clock, project_id=project_id, specification_id=spec_id
            )
        if kind == "readiness":
            return readiness_report(factory, _require(spec_id, "specificationId"), now=clock)
        if kind == "sessions":
            return active_sessions(factory, _require(project_id, "projectId"), now=clock)
        raise ValidationFailedError(
            f"get_report requires 'report' to be one of: {', '.join(_REPORT_KINDS)}",
            context={"failing_field": "report"},
        )

    @staticmethod
    def _scope_id(arguments: Mapping[str, Any], key: str, scope: str) -> Any:
        """Resolve an entity id from the explicit camelCase ``key`` or a matching scope."""
        direct = arguments.get(key)
        if direct:
            return direct
        if arguments.get("scope") == scope:
            scoped = arguments.get("scopeId")
            if scoped:
                return scoped
        return None

    # ---------------------------------------------------------------------------------- #
    # Utility — feedback (a local no-op acknowledgement).                                 #
    # ---------------------------------------------------------------------------------- #

    def _dispatch_feedback(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Acknowledge a `feedback` call locally (the AWS telemetry sink is dropped)."""
        payload: dict[str, Any] = {
            "tool": _FEEDBACK_TOOL,
            "status": "acknowledged",
            "message": "Feedback received locally — thank you.",
        }
        note = arguments.get("feedback") or arguments.get("message")
        if note is not None:
            payload["received"] = note
        return _as_dict(success_payload(payload))

    # ---------------------------------------------------------------------------------- #
    # Error normalisation — exception -> standard_error content.                          #
    # ---------------------------------------------------------------------------------- #

    @staticmethod
    def _normalise(
        exc: Exception, tool: str
    ) -> tuple[object, str, ErrorGuidance, dict[str, object] | None]:
        """Classify a raised exception, then map it to ``(code, message, guidance, context)``.

        A raw persistence failure is classified via
        :func:`~specsmither.operations.errors.to_crud_error` first (so a leaked
        ``IntegrityError`` lands on the right CRUD code); everything else flows straight
        through :func:`~specsmither.dispatch.error_guidance.normalise_error`.
        """
        classified: Exception = to_crud_error(exc) if isinstance(exc, IntegrityError) else exc
        return normalise_error(classified, tool=tool)

    @staticmethod
    def _error_envelope(
        code: object,
        message: str,
        guidance: ErrorGuidance | None,
        context: dict[str, object] | None,
    ) -> dict[str, Any]:
        """Build the ``standard_error`` envelope from a normalised error 4-tuple."""
        return standard_error_envelope(
            str(code), message, _guidance_to_dict(guidance), context
        )


def _arg(arguments: Mapping[str, Any], key: str) -> Any:
    """Read a wire argument as ``Any`` so it threads into the typed primitives.

    ``Mapping.get`` widens a missing key to ``Any | None``, which a non-optional ``str`` /
    ``int`` primitive parameter rejects under ``--strict``; the explicit ``Any`` return
    lets a genuinely-missing id flow to the primitive's own ``_require_id`` /
    ``NotFoundError`` validation (the single, English error-surfacing seam).
    """
    return arguments.get(key)


def _require(value: Any, name: str) -> Any:
    """Reject a missing required report scope id with a typed validation error."""
    if not value:
        raise ValidationFailedError(
            f"{name} is required for this report.", context={"failing_field": name}
        )
    return value


def make_dispatcher(
    session_factory: sessionmaker[Session],
    *,
    clock: Callable[[], datetime] | datetime | None = None,
) -> Dispatcher:
    """Construct a :class:`Dispatcher` over ``session_factory`` (the L6 MCP server seam)."""
    return Dispatcher(session_factory, clock=clock)
