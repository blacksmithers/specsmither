"""Regression locks for the specforge-lifecycle evolutions ported into specsmither.

Covers the "blocks-the-simulator" set — the fixes without which an unattended
draft->ready drive stalls or corrupts (see planner notes): #8 handover-gate persist,
MB.1.1 id/order filler, MB.1.2 field-shape guard, MB.2 relational link write + retarget,
#11a/broken_reference FK guards, #12 invalid-field surfacing, and the
structural_create_in_expansion soft-deny.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from specsmither.adapters.write_plan_executor import RelatedDelete, RelatedPut
from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.prechecks import (
    Accepted,
    Denied,
    blueprint_link_refs_exist,
    entity_refs_exist,
    field_shape_soft_deny,
    operation_allowed,
)
from specsmither.lifecycle.write_plan import (
    build_cps_success_write_plan,
    build_op_write_items,
    ensure_item_ids,
    extract_mutation_target,
)

# --------------------------------------------------------------------------- #
# #8 — CPS success persists last_gate_result='pass' (else approve deadlocks)   #
# --------------------------------------------------------------------------- #


def test_cps_success_persists_gate_pass() -> None:
    plan = build_cps_success_write_plan(
        session_id="s1",
        action={"id": "a1", "created_at": "2026-07-12T00:00:00Z"},
        transition={"id": "t1"},
    )
    session_updates = [i for i in plan.items if type(i).__name__ == "SessionUpdate"]
    assert session_updates
    fields = session_updates[0].fields
    assert fields["last_gate_result"] == "pass"
    assert fields["last_validated_at"] == "2026-07-12T00:00:00Z"


# --------------------------------------------------------------------------- #
# MB.1.1 — id/order filler mints schema-required ids (+order for AC)           #
# --------------------------------------------------------------------------- #


def test_ensure_item_ids_mints_ids_and_order() -> None:
    out = ensure_item_ids(
        {
            "goals": [{"title": "G"}],
            "acceptanceCriteria": [{"given": "g", "when": "w", "then": "t"}],
            "requirements": [{"title": "R", "acceptanceCriteria": [{"given": "a"}]}],
            "requirementsCovered": ["r-1"],  # string array — untouched
        },
        id_generator=lambda: "GID",
    )
    assert out["goals"][0]["id"] == "GID"
    assert out["acceptanceCriteria"][0]["id"] == "GID"
    assert out["acceptanceCriteria"][0]["order"] == 1
    assert out["requirements"][0]["id"] == "GID"
    assert out["requirements"][0]["acceptanceCriteria"][0]["id"] == "GID"
    assert out["requirements"][0]["acceptanceCriteria"][0]["order"] == 1
    assert out["requirementsCovered"] == ["r-1"]  # not an id-bearing array


def test_ensure_item_ids_lowercases_api_contract_type() -> None:
    out = ensure_item_ids({"apiContracts": [{"name": "X", "type": "REST"}]})
    assert out["apiContracts"][0]["type"] == "rest"


def test_mb11_round_trips_idless_content_through_the_store() -> None:
    with tempfile.TemporaryDirectory(prefix="ev-mb11-") as td:
        os.environ.update(SPECSMITHER_DB=str(Path(td) / "db.sqlite"), SPECSMITHER_HOME=td)
        from specsmither.db.base import make_session_factory
        from specsmither.db.migrations import init_db
        from specsmither.db.repositories.all_stores import make_stores
        from specsmither.dispatch.facade import make_dispatcher
        from specsmither.operations import crud, workspace

        init = workspace.init(cwd=td, project_name="ev", env=os.environ)
        factory = make_session_factory(init_db(os.environ["SPECSMITHER_DB"]))
        spec = crud.create_specification(factory, project_id=init.project_id, title="EV")
        disp = make_dispatcher(factory)
        sid = disp.dispatch("start_planning_session", {"specId": spec.id})["agent_response"][
            "session_id"
        ]
        disp.dispatch(
            "action_planning_session",
            {
                "sessionId": sid,
                "operation": "update_spec",
                "payload": {"fields": {"goals": [{"title": "G1", "description": "x", "type": "business"}]}},
            },
        )
        with factory() as session:
            rec = make_stores(session).specifications.get_specification(spec.id)
        assert rec is not None and rec.goals is not None
        assert rec.goals[0].id  # the read boundary no longer drops the id-less array


# --------------------------------------------------------------------------- #
# MB.2 — relational link write path + spec-target retarget                     #
# --------------------------------------------------------------------------- #


def test_mb2_link_emits_join_rows_and_retargets_to_spec() -> None:
    assert extract_mutation_target("link_blueprint_to_tickets", {"blueprintId": "bp"}, "SPEC") == (
        "spec",
        "SPEC",
    )
    link = build_op_write_items(
        "link_blueprint_to_tickets", {"blueprintId": "bp", "ticketIds": ["t1", "t2"]}
    )
    puts = [i for i in link.extra_items if isinstance(i, RelatedPut)]
    assert [p.item["id"] for p in puts] == ["t1-br-bp", "t2-br-bp"]
    assert all(p.table == "ticket_blueprint_refs" for p in puts)
    unlink = build_op_write_items(
        "unlink_blueprint_to_tickets", {"blueprintId": "bp", "ticketIds": ["t1"]}
    )
    dels = [i for i in unlink.extra_items if isinstance(i, RelatedDelete)]
    assert dels[0].key == {"id": "t1-br-bp"}


# --------------------------------------------------------------------------- #
# #12 — the adapter surfaces structural.invalid_fields as findings             #
# --------------------------------------------------------------------------- #


def test_adapter_surfaces_invalid_fields() -> None:
    import importlib

    from specsmither.adapters.crucible_validator import CrucibleValidatorAdapter

    t = importlib.import_module("tests.test_adapters_crucible_validator")
    out = CrucibleValidatorAdapter().validate(t._spec_full(), PlanningPhase.TICKET_EXPANSION, t._config())
    schema = [f for f in out.findings if "schema" in str(f.category).lower()]
    assert schema  # min-count / order invalid_fields are now surfaced, not dropped


# --------------------------------------------------------------------------- #
# FK-existence + shape prechecks                                               #
# --------------------------------------------------------------------------- #


def _spec_full() -> Any:
    from specsmither.lifecycle.ports import BlueprintRef, EpicFull, SpecFull, TicketRef

    ticket = TicketRef(id="t1", epic_id="e1", title="T1")
    epic = EpicFull(id="e1", specification_id="s1", title="E1", tickets=[ticket], description="d")
    return SpecFull(
        spec=type("S", (), {"id": "s1", "status": "planning"})(),
        epics=[epic],
        blueprints=[BlueprintRef(id="bp1", specification_id="s1", title="BP1", category="architecture")],
    )


def test_entity_refs_exist_denies_unknown_epic_on_create_ticket() -> None:
    denied = entity_refs_exist("create_ticket", {"epicId": "nope"}, _spec_full())
    assert isinstance(denied, Denied) and denied.code == "epic_not_found"
    assert entity_refs_exist("create_ticket", {"epicId": "e1"}, _spec_full()) == Accepted()


def test_entity_refs_exist_denies_broken_dependency_endpoint() -> None:
    denied = entity_refs_exist(
        "create_dependencies",
        {"dependencies": [{"fromTicketId": "t1", "toTicketId": "ghost"}]},
        _spec_full(),
    )
    assert isinstance(denied, Denied) and denied.code == "broken_reference"


def test_blueprint_link_refs_exist_denies_unknown_ids() -> None:
    denied = blueprint_link_refs_exist(
        "link_blueprint_to_tickets", {"blueprintId": "ghost", "ticketIds": ["t1"]}, _spec_full()
    )
    assert isinstance(denied, Denied) and denied.code == "dangling_reference"
    ok = blueprint_link_refs_exist(
        "link_blueprint_to_tickets", {"blueprintId": "bp1", "ticketIds": ["t1"]}, _spec_full()
    )
    assert ok == Accepted()


def test_field_shape_soft_deny_flags_bad_contract_type_and_missing_content() -> None:
    bad_type = field_shape_soft_deny("update_epic", {"fields": {"apiContracts": [{"type": "soap"}]}})
    assert isinstance(bad_type, Denied) and bad_type.code == "invalid_field_shape"
    # REST folds to rest -> accepted at the guard (the write boundary lower-cases it).
    assert isinstance(
        field_shape_soft_deny("update_epic", {"fields": {"apiContracts": [{"type": "REST"}]}}),
        Accepted,
    )
    missing = field_shape_soft_deny(
        "update_epic", {"fields": {"fileStructures": [{"scope": "epic"}]}}
    )
    assert isinstance(missing, Denied) and missing.code == "invalid_field_shape"


def test_structural_create_in_expansion_is_soft_denied() -> None:
    denied = operation_allowed("create_epic", PlanningPhase.EPIC_EXPANSION, {})
    assert isinstance(denied, Denied) and denied.code == "structural_create_in_expansion"
    # a create in its native decomposition phase is still accepted.
    assert isinstance(operation_allowed("create_epic", PlanningPhase.EPIC_DECOMPOSITION, {}), Accepted)


def test_content_shape_valid_rejects_poison_across_entities() -> None:
    from specsmither.lifecycle.prechecks import content_shape_valid

    # The universal write-boundary guard: content that would crash the read boundary
    # (poisoning every later action) is denied up front for spec, epic AND ticket, with
    # precise field errors — while well-formed content passes (no over-rejection).
    good_spec = {
        "fields": {
            "goals": [{"title": "G", "description": "d", "type": "business"}],
            "scope": {"inScope": ["a"], "outOfScope": ["b"]},
        }
    }
    assert isinstance(content_shape_valid("update_spec", good_spec), Accepted)

    # nested scope shape poison (a list-of-objects where strings are required)
    scope_poison = content_shape_valid(
        "update_spec", {"fields": {"scope": {"externalDependencies": [{"x": 1}]}}}
    )
    assert isinstance(scope_poison, Denied) and scope_poison.code == "invalid_content"

    # ticket testTypes enum poison + codeReferences shape poison
    tt = content_shape_valid("update_ticket", {"fields": {"testSpecification": {"testTypes": ["ci"]}}})
    assert isinstance(tt, Denied) and tt.blockers
    cr = content_shape_valid("update_ticket", {"fields": {"codeReferences": [{"note": "x"}]}})
    assert isinstance(cr, Denied) and any("filePath" in b for b in (cr.blockers or []))
    # well-formed ticket content passes
    assert isinstance(
        content_shape_valid(
            "update_ticket",
            {"fields": {"acceptanceCriteria": [{"given": "g", "when": "w", "then": "t"}]}},
        ),
        Accepted,
    )


def test_ticket_type_is_create_only() -> None:
    from specsmither.lifecycle.write_plan import build_create_persist

    sf = type("SF", (), {"spec": type("S", (), {"id": "s"})(), "epics": [type("E", (), {"id": "e1", "tickets": []})()], "blueprints": []})()
    # create honours ticketType='verification' (needed for the impl:verification ratio gate)
    cp = build_create_persist("create_ticket", {"epicId": "e1", "title": "V", "ticketType": "verification"}, sf)
    assert cp is not None and cp.fields["ticket_type"] == "verification"
    # default is implementation
    cp2 = build_create_persist("create_ticket", {"epicId": "e1", "title": "I"}, sf)
    assert cp2 is not None and cp2.fields["ticket_type"] == "implementation"


def test_enum_field_guards_deny_off_enum_values_together() -> None:
    from specsmither.lifecycle.prechecks import enum_field_guards

    # ME.14.2 — off-enum values in content arrays are denied cleanly (not an INTERNAL crash),
    # and ALL offenders are collected in one denial so the actor fixes them in one retry.
    denied = enum_field_guards(
        "update_spec",
        {
            "fields": {
                "techStack": [{"name": "py", "layer": "language"}],
                "nonFunctionalRequirements": [{"category": "portability"}],
                "guardrails": [{"category": "architecture"}],
            }
        },
    )
    assert isinstance(denied, Denied) and denied.code == "invalid_enum_value"
    assert denied.blockers is not None and len(denied.blockers) == 3
    # valid values pass.
    assert isinstance(
        enum_field_guards("update_spec", {"fields": {"techStack": [{"name": "py", "layer": "backend"}]}}),
        Accepted,
    )
