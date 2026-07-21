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


# --- end-to-end through the real dispatcher ----------------------------------------- #


def _seed_dispatcher(tmp_path: Path):  # type: ignore[no-untyped-def]
    import os

    from specsmither.adapters.lifecycle_ports import SqliteSpecStore
    from specsmither.db.base import make_session_factory
    from specsmither.db.migrations import init_db
    from specsmither.dispatch.facade import make_dispatcher
    from specsmither.operations import crud, workspace

    os.environ.update(
        SPECSMITHER_DB=str(tmp_path / "db.sqlite"), SPECSMITHER_HOME=str(tmp_path)
    )
    init = workspace.init(cwd=str(tmp_path), project_name="jf", env=os.environ)
    factory = make_session_factory(init_db(os.environ["SPECSMITHER_DB"]))
    spec = crud.create_specification(factory, project_id=init.project_id, title="JF")
    epic = crud.create_epic(factory, specification_id=spec.id, title="E1")
    ticket = crud.create_ticket(factory, epic_id=epic.id, title="T1")
    disp = make_dispatcher(factory)
    sid = disp.dispatch("start_planning_session", {"specId": spec.id})["agent_response"][
        "session_id"
    ]
    return disp, factory, SqliteSpecStore, spec.id, ticket.id, sid


def _ticket_decls(factory, spec_store_cls, spec_id):  # type: ignore[no-untyped-def]
    with factory() as session:
        sf = spec_store_cls(session).get_spec_full(spec_id)
    return sf.epics[0].tickets[0].extra.get("fieldDeclarations", {})


def test_justify_then_unjustify_round_trips_through_the_store(tmp_path: Path) -> None:
    disp, factory, store_cls, spec_id, ticket_id, sid = _seed_dispatcher(tmp_path)

    ok = disp.dispatch(
        "action_planning_session",
        {"sessionId": sid, "operation": "justify",
         "payload": {"entityId": ticket_id, "scope": "dependencies", "reason": _REASON}},
    )["agent_response"]
    assert ok["outcome"] == "success"
    assert _ticket_decls(factory, store_cls, spec_id).get("dependencies", {}).get("value") == "N/A"

    cleared = disp.dispatch(
        "action_planning_session",
        {"sessionId": sid, "operation": "unjustify",
         "payload": {"entityId": ticket_id, "scope": "dependencies"}},
    )["agent_response"]
    assert cleared["outcome"] == "success"
    assert "dependencies" not in _ticket_decls(factory, store_cls, spec_id)


def test_justify_denials_flow_through_the_dispatcher(tmp_path: Path) -> None:
    disp, _factory, _store, _spec, ticket_id, sid = _seed_dispatcher(tmp_path)
    del _factory, _store, _spec

    short = disp.dispatch(
        "action_planning_session",
        {"sessionId": sid, "operation": "justify",
         "payload": {"entityId": ticket_id, "scope": "dependencies", "reason": "nope"}},
    )["agent_response"]
    assert short["outcome"] == "denied"

    bad_scope = disp.dispatch(
        "action_planning_session",
        {"sessionId": sid, "operation": "justify",
         "payload": {"entityId": ticket_id, "scope": "title", "reason": _REASON}},
    )["agent_response"]
    assert bad_scope["outcome"] == "denied"
