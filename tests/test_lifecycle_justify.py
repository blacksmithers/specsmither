"""Unit coverage for the justify/unjustify foundation (MB.11): the resolver + the
agent-wire fieldDeclarations strip.

These are the two self-contained pieces of the justify port; the op-routing that wires
them into action_planning_session lands with the registry/schema/carve-out activation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pure_harness import demo_spec_full
from specsmither.lifecycle.prechecks.result import Denied
from specsmither.lifecycle.prechecks.strip_field_declarations import (
    strip_agent_wire_field_declarations,
)
from specsmither.lifecycle.verbs.justify_support import (
    JustifyResolution,
    resolve_justify,
)

_MIN = 20
_REASON = "this dependency is genuinely not applicable here"


def test_justify_ticket_scope_merges_declaration() -> None:
    sf = demo_spec_full()
    out = resolve_justify("justify", {"entityId": "ticket-a", "scope": "dependencies", "reason": _REASON}, sf, _MIN)
    assert isinstance(out, JustifyResolution)
    assert out.entity_type == "ticket"
    assert out.entity_id == "ticket-a"
    assert out.field_declarations["dependencies"] == {"value": "N/A", "reason": _REASON}


def test_justify_epic_and_spec_resolve_by_id() -> None:
    sf = demo_spec_full()
    epic = resolve_justify("justify", {"entityId": "epic-0001", "scope": "apiContracts", "reason": _REASON}, sf, _MIN)
    assert isinstance(epic, JustifyResolution) and epic.entity_type == "epic"
    spec = resolve_justify("justify", {"entityId": "spec-0001", "scope": "guardrails", "reason": _REASON}, sf, _MIN)
    assert isinstance(spec, JustifyResolution) and spec.entity_type == "spec"


def test_unjustify_removes_the_scope_key() -> None:
    sf = demo_spec_full()
    out = resolve_justify("unjustify", {"entityId": "ticket-a", "scope": "dependencies"}, sf, _MIN)
    assert isinstance(out, JustifyResolution)
    assert "dependencies" not in out.field_declarations


def test_justify_denies_unknown_entity() -> None:
    sf = demo_spec_full()
    out = resolve_justify("justify", {"entityId": "ghost", "scope": "dependencies", "reason": _REASON}, sf, _MIN)
    assert isinstance(out, Denied) and out.code == "ticket_not_found"


def test_justify_denies_non_na_eligible_scope() -> None:
    sf = demo_spec_full()
    out = resolve_justify("justify", {"entityId": "ticket-a", "scope": "title", "reason": _REASON}, sf, _MIN)
    assert isinstance(out, Denied) and out.code == "scope_not_na_eligible"


def test_justify_denies_short_reason() -> None:
    sf = demo_spec_full()
    out = resolve_justify("justify", {"entityId": "ticket-a", "scope": "dependencies", "reason": "too short"}, sf, _MIN)
    assert isinstance(out, Denied) and out.code == "justification_too_short"


# --- strip -------------------------------------------------------------------------- #


def test_strip_removes_top_level_and_nested_field_declarations() -> None:
    top = strip_agent_wire_field_declarations(
        "update_ticket", {"id": "t1", "fields": {"description": "d"}, "fieldDeclarations": {"x": {}}}
    )
    assert "fieldDeclarations" not in top
    nested = strip_agent_wire_field_declarations(
        "update_ticket", {"id": "t1", "fields": {"description": "d", "fieldDeclarations": {"x": {}}}}
    )
    assert "fieldDeclarations" not in nested["fields"]
    assert nested["fields"]["description"] == "d"


def test_strip_is_a_noop_for_justify() -> None:
    payload = {"entityId": "t1", "scope": "dependencies", "reason": _REASON}
    assert strip_agent_wire_field_declarations("justify", payload) == payload
