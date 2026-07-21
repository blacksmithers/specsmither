"""The agent-wire clean break for ``fieldDeclarations``.

N/A justification used to ride the ``update_*`` / ``create_ticket`` / ``create_epic``
payloads by overloading a ``fieldDeclarations`` key. N/A now has its own dedicated,
structural-neutral ops (``justify`` / ``unjustify``, which write the canonical bare
``fieldDeclarations[scope]`` key server-side and never roll back), so ``fieldDeclarations``
is stripped from every one of these payloads BEFORE it can reach the persisted
``SpecMutation`` fields OR the projector — keeping persist and projection symmetric.

The store-level writer legitimately keeps its ``fieldDeclarations`` column — that is the
shared persistence ``justify`` reuses. The invariant is "no agent-wire op other than
``justify`` writes ``fieldDeclarations``", not "no writer exists".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName

__all__ = ["strip_agent_wire_field_declarations"]

#: The agent-wire ops that used to overload ``fieldDeclarations``. ``justify`` / ``unjustify``
#: are excluded — they own the canonical key.
_AGENT_WIRE_DECL_OPS: frozenset[str] = frozenset(
    {"update_spec", "update_epic", "update_ticket", "create_ticket", "create_epic"}
)


def strip_agent_wire_field_declarations(
    op: PlanningOperationName, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return ``payload`` with any ``fieldDeclarations`` removed (a fresh dict).

    Covers BOTH the nested shape ``{fields: {fieldDeclarations: {…}}}`` and the top-level
    shape ``{fields: {…}, fieldDeclarations: {…}}`` the retired guidance taught. A no-op
    (returns an equivalent dict) for ``justify`` / ``unjustify`` and for any payload that
    carries no ``fieldDeclarations``.
    """
    base: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
    if op not in _AGENT_WIRE_DECL_OPS:
        return base
    # top-level shape `{fields: {…}, fieldDeclarations: {…}}`
    base.pop("fieldDeclarations", None)
    # nested shape `{fields: {fieldDeclarations: {…}}}`
    fields = base.get("fields")
    if isinstance(fields, Mapping) and "fieldDeclarations" in fields:
        base["fields"] = {k: v for k, v in fields.items() if k != "fieldDeclarations"}
    return base
