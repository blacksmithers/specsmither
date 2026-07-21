"""The three wire CONTENT shapes the dispatch facade returns.

The JSON-able *content* the facade hands back and the MCP server TOON-encodes.
Deliberately **no JSON-RPC / HTTP transport code map**: a domain error here is
*content* (a ``standard_error`` envelope), never a protocol error, so there is no
transport code table (HTTP status + JSON-RPC code) — error codes stay content-level.

A client dispatches on ``kind`` first. There are exactly three results:

#. **lifecycle** — :class:`LifecycleEnvelope` ``{kind: 'lifecycle', agent_response}``. Used
   on BOTH the success AND the denial branch of a planning verb: a denial
   *self-discriminates* because the embedded :class:`PlanningAgentResponse` carries
   ``outcome == 'denied'`` — there is no separate denial code, no separate envelope. The
   response is embedded *verbatim, field-preserving* (no allow/deny-list, no remap); the
   only addition is the derived ``gate_passed`` boolean, reconstituted from the
   ``gate_result`` literal so clients need not re-derive it.
#. **success** — the RAW impl payload with NO envelope wrapper. :func:`success_payload` is
   essentially identity: it just normalises the value to a plain JSON-able structure
   (enums → ``.value``, dataclasses / pydantic records → dicts, datetimes → ISO strings).
#. **error** — :class:`StandardErrorEnvelope`
   ``{kind: 'standard_error', code, message, guidance, context}``. Only thrown errors are
   enveloped; ``guidance`` is the composer-produced :class:`ErrorGuidance` dict (``None``
   for ``INTERNAL`` — a bare exception has no actionable recovery hint).

The four work verbs (``start`` / ``action`` / ``complete`` / ``reset_work_session``) are not
implemented in 0.1.0; they resolve — never throw — with a :class:`StubResponse`
``{status: 'in_development', planned_for: '0.2.0', …}`` so clients render a "coming soon"
affordance instead of catching an exception.

Everything returned by the factory functions is a plain, JSON-serialisable dict (or, for
:func:`success_payload`, a JSON-able value) — the single boundary contract the TOON
encoder relies on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from specsmither.lifecycle.guidance.types import PlanningAgentResponse

__all__ = [
    "DEFAULT_RESPONSE_DETAIL",
    "ERROR_GUIDANCE_CODES",
    "RESPONSE_DETAILS",
    "STUB_SINCE",
    "WORK_PLANNED_FOR",
    "WORK_VERBS",
    "ErrorGuidanceCode",
    "LifecycleEnvelope",
    "ResponseDetail",
    "StandardErrorEnvelope",
    "StubResponse",
    "lifecycle_envelope",
    "normalise_response_detail",
    "standard_error_envelope",
    "stub_response",
    "success_payload",
    "to_jsonable",
]


# --------------------------------------------------------------------------------------
# Response verbosity tier (declared in the tool schema; forwarded into the lifecycle
# payload, where the PlanningAgentResponse builder is the one that actually honours it —
# the dispatch facade only passes it through).
# --------------------------------------------------------------------------------------

#: The three response-verbosity tiers (~80 / ~200 / ~500 tokens); ``standard`` default.
ResponseDetail = Literal["minimal", "standard", "full"]

#: The tier vocabulary, in widening order — handy for tool-schema ``enum`` generation.
RESPONSE_DETAILS: tuple[ResponseDetail, ...] = ("minimal", "standard", "full")

#: The default tier when a caller omits / mis-spells ``responseDetail``.
DEFAULT_RESPONSE_DETAIL: ResponseDetail = "standard"


def normalise_response_detail(value: object) -> ResponseDetail:
    """Coerce an arbitrary wire value to a :data:`ResponseDetail` (default ``standard``).

    Unknown / missing values fall back to :data:`DEFAULT_RESPONSE_DETAIL` (the tool-schema
    default) — the facade calls this on the raw ``responseDetail`` arg before forwarding it
    into the lifecycle payload.
    """
    if value == "minimal":
        return "minimal"
    if value == "full":
        return "full"
    return "standard"


# --------------------------------------------------------------------------------------
# JSON-able normalisation — the single boundary rule: enums → .value, dataclasses /
# pydantic records → dicts, datetimes → ISO strings, nested structures recursed.
# --------------------------------------------------------------------------------------


def to_jsonable(value: object) -> object:
    """Recursively normalise ``value`` to a plain JSON-serialisable structure.

    The boundary contract the TOON encoder relies on: :class:`~enum.Enum` →
    ``.value``; :class:`~pydantic.BaseModel` → its camelCase wire dict (same convention
    as the operations layer's ``_to_wire``); a frozen dataclass → a field dict;
    :class:`~datetime.date` / :class:`~datetime.datetime` → ISO-8601; mappings and
    list/tuple/set sequences recursed; scalars (``None`` / ``bool`` / ``int`` / ``float``
    / ``str``) passed through unchanged. ``Enum`` is tested *before* the scalar branch
    because ``StrEnum`` / ``IntEnum`` members are ``str`` / ``int`` instances.
    """
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    return value


def _jsonable_dict(value: Mapping[str, object] | None) -> dict[str, object] | None:
    """Normalise an optional string-keyed mapping to a JSON-able ``dict`` (or ``None``)."""
    if value is None:
        return None
    return {str(key): to_jsonable(val) for key, val in value.items()}


def _code_value(code: str) -> str:
    """Resolve an error code to its plain string value (``Enum`` member → ``.value``)."""
    if isinstance(code, Enum):
        return str(code.value)
    return code


# --------------------------------------------------------------------------------------
# 1. Lifecycle envelope — success AND denial ride the same shape; denial self-
#    discriminates via the embedded agent_response.outcome == 'denied'.
# --------------------------------------------------------------------------------------


def _agent_response_to_dict(response: PlanningAgentResponse) -> dict[str, object]:
    """Embed a :class:`PlanningAgentResponse` verbatim as a JSON-able dict.

    Field-preserving by construction — every dataclass field is emitted, with the derived
    ``gate_passed`` boolean added so clients can read the typed gate verdict directly
    without re-deriving it from ``gate_result``.
    """
    data: dict[str, object] = {
        f.name: to_jsonable(getattr(response, f.name)) for f in fields(response)
    }
    data["gate_passed"] = response.gate_passed
    return data


@dataclass(frozen=True)
class LifecycleEnvelope:
    """Wire envelope for the lifecycle (planning) verbs — ``McpLifecycleEnvelope``.

    Carries the typed :class:`PlanningAgentResponse` whole; :meth:`to_content` emits the
    JSON-able ``{kind: 'lifecycle', agent_response}`` dict. The same shape is returned on
    both success and denial (the latter via ``agent_response.outcome == 'denied'``).
    """

    agent_response: PlanningAgentResponse
    kind: Literal["lifecycle"] = "lifecycle"

    def to_content(self) -> dict[str, object]:
        """Render the JSON-able lifecycle content dict (the TOON encoder's input)."""
        return {
            "kind": self.kind,
            "agent_response": _agent_response_to_dict(self.agent_response),
        }


def lifecycle_envelope(response: PlanningAgentResponse) -> dict[str, object]:
    """Wrap a planning verb's :class:`PlanningAgentResponse` in the lifecycle envelope."""
    return LifecycleEnvelope(agent_response=response).to_content()


# --------------------------------------------------------------------------------------
# 2. Bare success payload — the standard tools return their raw data, NO envelope.
# --------------------------------------------------------------------------------------


def success_payload(data: object) -> object:
    """Return ``data`` as a bare, un-enveloped, JSON-able payload (essentially identity).

    Standard query / operation success carries NO ``kind`` discriminator — the raw impl
    payload IS the result. This only normalises the value to a plain JSON-able structure
    (see :func:`to_jsonable`); it never wraps, never adds a discriminator.
    """
    return to_jsonable(data)


# --------------------------------------------------------------------------------------
# 3. Standard error envelope — only thrown errors are enveloped; success is bare.
# --------------------------------------------------------------------------------------

#: The seven MCP error codes a public tool can surface. There is deliberately no transport
#: code map (HTTP status / JSON-RPC code) — these are content errors, never protocol errors.
ErrorGuidanceCode = Literal[
    "NOT_FOUND",
    "VALIDATION_FAILED",
    "CONFLICT",
    "PRECONDITION_FAILED",
    "UNAUTHORISED",
    "UNKNOWN_TOOL",
    "INTERNAL",
]

#: The error-code vocabulary as a tuple (membership tests / schema generation).
ERROR_GUIDANCE_CODES: tuple[ErrorGuidanceCode, ...] = (
    "NOT_FOUND",
    "VALIDATION_FAILED",
    "CONFLICT",
    "PRECONDITION_FAILED",
    "UNAUTHORISED",
    "UNKNOWN_TOOL",
    "INTERNAL",
)


@dataclass(frozen=True)
class StandardErrorEnvelope:
    """Wire envelope for the standard (non-lifecycle) tools on the ERROR path.

    ``guidance`` is the composer-produced :class:`ErrorGuidance` dict (``{prose,
    next_actions?, related?}``) and is ``None`` for ``INTERNAL`` (a bare exception has no
    actionable recovery hint). ``context`` carries the typed extras that fed the composer
    (the not-found id, the conflicting entity, the failing field, …) so clients can render
    structured affordances alongside the prose.
    """

    code: str
    message: str
    guidance: dict[str, object] | None = None
    context: dict[str, object] | None = None
    kind: Literal["standard_error"] = "standard_error"

    def to_content(self) -> dict[str, object]:
        """Render the JSON-able ``standard_error`` content dict."""
        return {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "guidance": self.guidance,
            "context": self.context,
        }


def standard_error_envelope(
    code: str,
    message: str,
    guidance: Mapping[str, object] | None = None,
    context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the ``standard_error`` envelope dict from a code, message, and guidance.

    ``code`` accepts a plain string or any ``Enum`` member (e.g. ``CrudErrorCode``); it is
    resolved to its string ``.value``. ``guidance`` is the composer output (an
    :class:`ErrorGuidance` dict) or ``None``; both it and ``context`` are normalised to
    plain JSON-able dicts.
    """
    return StandardErrorEnvelope(
        code=_code_value(code),
        message=message,
        guidance=_jsonable_dict(guidance),
        context=_jsonable_dict(context),
    ).to_content()


# --------------------------------------------------------------------------------------
# 4. Stub response — the four 0.1.0-unimplemented work verbs resolve, never throw.
# --------------------------------------------------------------------------------------

#: The release that introduced these stubs (the ``since`` audit field).
STUB_SINCE = "0.1.0"

#: The release the work-session verbs are planned for (the ``planned_for`` field).
WORK_PLANNED_FOR = "0.2.0"

#: The four work-session verbs stubbed in 0.1.0 (real handlers land in 0.2.0).
WORK_VERBS: tuple[str, ...] = (
    "start_work_session",
    "action_work_session",
    "complete_work_session",
    "reset_work_session",
)


@dataclass(frozen=True)
class StubResponse:
    """The "coming soon" payload for a not-yet-implemented work verb (``StubResponse``).

    Resolves — never throws — so clients branch on ``status == 'in_development'`` and
    render a "coming soon" affordance. ``since`` records the release that introduced the
    stub; ``planned_for`` the release the real handler lands in.
    """

    verb: str
    message: str
    planned_for: str = WORK_PLANNED_FOR
    since: str = STUB_SINCE
    status: Literal["in_development"] = "in_development"

    def to_content(self) -> dict[str, object]:
        """Render the JSON-able stub content dict (``status`` is the discriminant)."""
        return {
            "status": self.status,
            "verb": self.verb,
            "message": self.message,
            "since": self.since,
            "planned_for": self.planned_for,
        }


def stub_response(verb: str, *, planned_for: str = WORK_PLANNED_FOR) -> dict[str, object]:
    """Build the ``in_development`` stub dict for an unimplemented work ``verb``."""
    message = (
        f"`{verb}` is not implemented in {STUB_SINCE}. "
        f"The work-session verbs are planned for {planned_for}; "
        "branch on status == 'in_development' until the real handler lands."
    )
    return StubResponse(verb=verb, message=message, planned_for=planned_for).to_content()
