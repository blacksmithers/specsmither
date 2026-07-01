"""The error-guidance layer of the dispatch facade — work item #13.

A re-authored (English) port of ``api-types/mcp/error-guidance.ts`` plus the
``core/src/error-guidance/crud/*`` per-code composers. The TS prose is pt-BR and
its ``next_actions`` are structured ``{tool, args?, reason?}`` objects; this port
**re-authors the prose in English** and flattens ``next_actions`` / ``related``
to plain English imperative sentences (``list[str]``) — the structured ``code`` /
``context`` are language-independent and remain the load-bearing contract.

Three public seams, all consumed by the standard-error envelope the facade emits
(``{kind: 'standard_error', code, message, guidance, context?}``):

* :class:`ErrorGuidanceCode` — the seven MCP error codes (the four
  :class:`~specsmither.operations.errors.CrudErrorCode` codes plus ``UNAUTHORISED``
  / ``UNKNOWN_TOOL`` / ``INTERNAL``).
* :func:`compose_error_guidance` — the per-code English composer producing the
  agent-facing :class:`ErrorGuidance` (canonical ``prose`` read verbatim by
  clients, plus actionable ``next_actions``).
* :func:`normalise_error` (and the :func:`unknown_tool_guidance` pre-check) — the
  single mapper that turns a raised exception into ``(code, message, guidance,
  context)`` for the envelope. It mirrors the TS dispatcher's branch order:
  a :class:`~specsmither.operations.errors.CrudError` maps to its matching code
  and composer; any other exception (engine :class:`SpecSmitherError` or a bare
  stdlib error) falls through to ``INTERNAL``; the unknown-tool case is a
  pre-dispatch path that lists the valid tools.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from specsmither.operations.errors import CrudError, CrudErrorCode

__all__ = [
    "ErrorGuidance",
    "ErrorGuidanceCode",
    "compose_error_guidance",
    "normalise_error",
    "unknown_tool_guidance",
]


class ErrorGuidanceCode(StrEnum):
    """The seven MCP error codes a public tool can surface.

    The first four are the wire-identical :class:`CrudErrorCode` values (they
    round-trip on the envelope); ``UNAUTHORISED`` / ``UNKNOWN_TOOL`` / ``INTERNAL``
    extend the surface for the dispatch facade's non-CRUD failure modes.
    """

    NOT_FOUND = "NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CONFLICT = "CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    UNAUTHORISED = "UNAUTHORISED"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class ErrorGuidance:
    """Agent-facing guidance for a failed tool call.

    ``prose`` is the canonical, agent-directed message — clients render it
    verbatim and never parse it (same convention as the lifecycle layer's
    ``PlanningAgentResponse.guidance``). ``next_actions`` are concrete English
    next-step sentences; ``related`` are optional pointers to adjacent tools. Both
    lists are flattened to strings (the TS ``{tool, args?, reason?}`` structure is
    folded into the sentence prose).
    """

    prose: str
    next_actions: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Per-entity copy (re-authored English labels + recovery verb).               #
# --------------------------------------------------------------------------- #

#: ``entity_type`` -> (English label, the verb that lists ids of that type).
_ENTITY_COPY: dict[str, tuple[str, str]] = {
    "spec": ("specification", "list"),
    "epic": ("epic", "list"),
    "ticket": ("ticket", "list"),
    "planning_session": ("planning session", "get_planning_status"),
    "blueprint": ("blueprint", "list"),
    "project": ("project", "list"),
    "pull_request": ("pull request", "list"),
    "dependency": ("dependency", "get_dependency_tree"),
    "ticket_blueprint_ref": ("ticket-blueprint link", "get_ticket_blueprints"),
}


def _entity_copy(entity_type: str | None) -> tuple[str, str]:
    """Resolve the (label, recovery-tool) pair, falling back to a generic entity."""

    if entity_type is not None and entity_type in _ENTITY_COPY:
        return _ENTITY_COPY[entity_type]
    return ("entity", "list")


def _ctx_str(context: dict[str, object] | None, *keys: str, default: str) -> str:
    """First present, non-``None`` value among ``keys`` (camel/snake), as ``str``.

    The composer accepts both the TS-origin camelCase keys (``entityId``,
    ``conflictingEntityId``, …) and snake_case variants, so it works whether the
    context was synthesised here or carried on a directly-constructed
    :class:`CrudError`.
    """

    if context:
        for key in keys:
            value = context.get(key)
            if value is not None:
                return str(value)
    return default


# --------------------------------------------------------------------------- #
# Per-code English composers.                                                  #
# --------------------------------------------------------------------------- #

_Composer = Callable[[str | None, str | None, "dict[str, object] | None"], ErrorGuidance]


def _compose_not_found(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    label, recovery = _entity_copy(entity_type)
    entity_id = _ctx_str(context, "entityId", "entity_id", "id", default="(unknown)")
    tool = _ctx_str(context, "tool", default="this operation")
    prose = (
        f"The {label} you referenced (`{entity_id}`) does not exist while running "
        f"`{tool}`. Call `{recovery}` to list the available ids and confirm the id "
        f"is correct."
    )
    return ErrorGuidance(
        prose=prose,
        next_actions=[
            f"Call `{recovery}` to enumerate valid {label} ids.",
            "Re-check the id you passed for typos, then retry.",
        ],
    )


def _compose_validation_failed(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="this operation")
    failing_field = _ctx_str(context, "failingField", "failing_field", "field", default="")
    if failing_field:
        head = f"The argument `{failing_field}` failed validation while running `{tool}`."
    else:
        detail = message or _ctx_str(context, "detail", default="")
        head = f"An argument failed validation while running `{tool}`."
        if detail:
            head = f"{head} {detail}"
    prose = (
        f"{head} Review the tool's input schema in the published tool catalog and "
        f"correct the argument before retrying."
    )
    return ErrorGuidance(
        prose=prose,
        next_actions=[
            "Review the tool's input schema in the published tool catalog.",
            "Correct the failing argument, then retry.",
        ],
    )


def _compose_conflict(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="this operation")
    entity_id = _ctx_str(context, "entityId", "entity_id", "id", default="(unknown)")
    conflicting = _ctx_str(
        context, "conflictingEntityId", "conflicting_entity_id", default="(unknown)"
    )

    if entity_type == "planning_session":
        prose = (
            f"An active planning session (`{conflicting}`) already exists for "
            f"`{entity_id}`. Complete it before starting another."
        )
        actions = [
            f"Call `complete_planning_session` to finish the active session `{conflicting}`.",
        ]
    elif entity_type == "dependency":
        prose = (
            f"`{tool}` failed: the dependency `{entity_id}` -> `{conflicting}` already "
            f"exists or would create a cycle in the graph. Adjust the graph or remove "
            f"the conflicting edge."
        )
        actions = [
            "Remove the conflicting dependency, then recreate it.",
            "Call `get_dependency_tree` to inspect the current graph.",
        ]
    elif entity_type == "ticket_blueprint_ref":
        prose = (
            f"`{tool}` failed: ticket `{entity_id}` is already linked to blueprint "
            f"`{conflicting}`. Unlink it before recreating the link."
        )
        actions = ["Unlink the existing blueprint reference, then recreate it."]
    else:
        prose = (
            f"`{tool}` failed: a conflicting resource (`{conflicting}`) already exists "
            f"for `{entity_id}`. Resolve the conflict before retrying."
        )
        actions = ["Resolve the conflicting resource, then retry."]

    return ErrorGuidance(prose=prose, next_actions=actions)


def _compose_precondition_failed(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="this operation")
    label, _recovery = _entity_copy(entity_type)
    entity_id = _ctx_str(context, "entityId", "entity_id", "id", default="(unknown)")
    expected = _ctx_str(context, "expectedStatus", "expected_status", default="(unknown)")
    actual = _ctx_str(context, "actualStatus", "actual_status", default="(unknown)")
    prose = (
        f"`{tool}` requires status `{expected}`, but the {label} `{entity_id}` is "
        f"currently `{actual}`. Consider `reopen_specification` first to return the "
        f"entity to an actionable state."
    )
    return ErrorGuidance(
        prose=prose,
        next_actions=[
            "Call `reopen_specification` to return the specification to an actionable state.",
            "Verify the entity's current status before retrying.",
        ],
    )


def _compose_unauthorised(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="this operation")
    scope = _ctx_str(context, "requiredScope", "required_scope", default="")
    if scope:
        prose = (
            f"You are not authorised to run `{tool}`; it requires the `{scope}` scope "
            f"or permission."
        )
    else:
        prose = f"You are not authorised to run `{tool}`."
    return ErrorGuidance(
        prose=prose,
        next_actions=["Confirm you hold the permission or scope this operation requires."],
    )


def _compose_unknown_tool(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="(unknown)")
    available = None
    if context:
        available = context.get("availableTools") or context.get("available_tools")
    if isinstance(available, (list, tuple)) and available:
        listed = ", ".join(f"`{name}`" for name in available)
        prose = f"There is no tool named `{tool}`. The available tools are: {listed}."
        actions = [
            "Choose one of the listed tools and retry.",
            "Inspect the published tool catalog for exact names and input schemas.",
        ]
    else:
        prose = (
            f"There is no tool named `{tool}`. Consult the published tool catalog for "
            f"the valid tool names."
        )
        actions = ["Consult the published tool catalog for valid tool names, then retry."]
    return ErrorGuidance(prose=prose, next_actions=actions)


def _compose_internal(
    entity_type: str | None, message: str | None, context: dict[str, object] | None
) -> ErrorGuidance:
    tool = _ctx_str(context, "tool", default="the requested operation")
    detail = message or _ctx_str(context, "detail", default="")
    prose = (
        f"An internal error occurred while running `{tool}` and the operation could "
        f"not be completed."
    )
    if detail:
        prose = f"{prose} Detail: {detail}."
    prose = (
        f"{prose} This is most likely a bug; retry once, and if it persists report it "
        f"with the attached context."
    )
    return ErrorGuidance(
        prose=prose,
        next_actions=[
            "Retry the operation once.",
            "If the error persists, report it with the attached context.",
        ],
    )


_COMPOSERS: dict[ErrorGuidanceCode, _Composer] = {
    ErrorGuidanceCode.NOT_FOUND: _compose_not_found,
    ErrorGuidanceCode.VALIDATION_FAILED: _compose_validation_failed,
    ErrorGuidanceCode.CONFLICT: _compose_conflict,
    ErrorGuidanceCode.PRECONDITION_FAILED: _compose_precondition_failed,
    ErrorGuidanceCode.UNAUTHORISED: _compose_unauthorised,
    ErrorGuidanceCode.UNKNOWN_TOOL: _compose_unknown_tool,
    ErrorGuidanceCode.INTERNAL: _compose_internal,
}


def compose_error_guidance(
    code: ErrorGuidanceCode,
    *,
    entity_type: str | None = None,
    message: str | None = None,
    context: dict[str, object] | None = None,
) -> ErrorGuidance:
    """Compose the English :class:`ErrorGuidance` for ``code``.

    Routes on ``code`` to the matching per-code composer (every code is covered;
    there is no fallback branch). ``entity_type`` selects per-entity copy where it
    matters (NOT_FOUND / CONFLICT / PRECONDITION_FAILED); ``message`` is a raw
    fallback fragment (used by VALIDATION_FAILED / INTERNAL when no structured
    field is present); ``context`` supplies the interpolation values (``tool``,
    ``entityId``, ``conflictingEntityId``, ``failingField``, ``expectedStatus`` /
    ``actualStatus``, ``requiredScope``, ``availableTools`` — camel or snake).
    Always returns non-empty ``prose`` and at least one ``next_action``.
    """

    return _COMPOSERS[code](entity_type, message, context)


# --------------------------------------------------------------------------- #
# The exception -> envelope mapper.                                            #
# --------------------------------------------------------------------------- #

#: CRUD code -> the wire-identical guidance code (the four shared values).
_CRUD_TO_GUIDANCE: dict[CrudErrorCode, ErrorGuidanceCode] = {
    CrudErrorCode.NOT_FOUND: ErrorGuidanceCode.NOT_FOUND,
    CrudErrorCode.VALIDATION_FAILED: ErrorGuidanceCode.VALIDATION_FAILED,
    CrudErrorCode.CONFLICT: ErrorGuidanceCode.CONFLICT,
    CrudErrorCode.PRECONDITION_FAILED: ErrorGuidanceCode.PRECONDITION_FAILED,
}


def _entity_type_from_context(context: dict[str, object] | None) -> str | None:
    """Pull a declared ``entityType`` (camel or snake) off a CrudError context."""

    if not context:
        return None
    for key in ("entityType", "entity_type", "entity"):
        value = context.get(key)
        if isinstance(value, str):
            return value
    return None


def _with_tool(context: dict[str, object] | None, tool: str | None) -> dict[str, object]:
    """A shallow copy of ``context`` with ``tool`` injected (never mutates input)."""

    merged: dict[str, object] = dict(context) if context else {}
    if tool is not None:
        merged.setdefault("tool", tool)
    return merged


def _internal_context(exc: Exception) -> dict[str, object]:
    """Build the INTERNAL context: the exception class name + any carried context."""

    context: dict[str, object] = {"exception": type(exc).__name__}
    existing = getattr(exc, "context", None)
    if isinstance(existing, dict):
        for key, value in existing.items():
            context.setdefault(str(key), value)
    return context


def normalise_error(
    exc: Exception, *, tool: str | None = None
) -> tuple[ErrorGuidanceCode, str, ErrorGuidance, dict[str, object] | None]:
    """Map a raised exception to ``(code, message, guidance, context)``.

    The single assembly seam feeding the facade's standard-error envelope. Branch
    order mirrors the TS dispatcher:

    * a :class:`~specsmither.operations.errors.CrudError` maps to its matching
      :class:`ErrorGuidanceCode` (``NotFoundError`` -> ``NOT_FOUND``,
      ``ValidationFailedError`` -> ``VALIDATION_FAILED``, ``ConflictError`` ->
      ``CONFLICT``, ``PreconditionFailedError`` -> ``PRECONDITION_FAILED``) and is
      composed from its ``message`` / ``context``; any English ``next_actions`` the
      verb attached are appended (deduplicated) after the composed ones.
    * any other exception — an engine :class:`SpecSmitherError` that is not a
      ``CrudError``, or a bare stdlib error — falls through to ``INTERNAL``,
      surfacing its ``str(exc)`` message and a context bag carrying the exception
      class name (plus any context it carried).

    The unknown-tool case is handled before dispatch by
    :func:`unknown_tool_guidance`, not here.
    """

    if isinstance(exc, CrudError):
        code = _CRUD_TO_GUIDANCE[exc.code]
        entity_type = _entity_type_from_context(exc.context)
        guidance = compose_error_guidance(
            code,
            entity_type=entity_type,
            message=exc.message,
            context=_with_tool(exc.context, tool),
        )
        if exc.next_actions:
            extra = [a for a in exc.next_actions if a not in guidance.next_actions]
            if extra:
                guidance = replace(guidance, next_actions=[*guidance.next_actions, *extra])
        return code, exc.message, guidance, exc.context

    # Engine SpecSmitherError (non-CRUD) or arbitrary exception -> INTERNAL.
    message = str(exc) or "Internal error"
    context = _internal_context(exc)
    guidance = compose_error_guidance(
        ErrorGuidanceCode.INTERNAL,
        message=message,
        context=_with_tool(context, tool),
    )
    return ErrorGuidanceCode.INTERNAL, message, guidance, context


def unknown_tool_guidance(
    tool: str, valid_tools: Iterable[str]
) -> tuple[ErrorGuidanceCode, str, ErrorGuidance, dict[str, object] | None]:
    """The pre-dispatch path for an unrecognised verb -> ``UNKNOWN_TOOL``.

    Returns the same 4-tuple shape as :func:`normalise_error` so the facade can
    feed it into the standard-error envelope uniformly. The guidance lists the
    valid tools (a "did you mean" hint); ``context`` carries them under
    ``availableTools`` for structured clients.
    """

    tools = list(valid_tools)
    context: dict[str, object] = {"tool": tool, "availableTools": tools}
    guidance = compose_error_guidance(ErrorGuidanceCode.UNKNOWN_TOOL, context=context)
    if tools:
        message = f"Unknown tool '{tool}'. Valid tools: {', '.join(tools)}."
    else:
        message = f"Unknown tool '{tool}'."
    return ErrorGuidanceCode.UNKNOWN_TOOL, message, guidance, context
