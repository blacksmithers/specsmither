"""Catalog parity: the public ``action_planning_session`` ``oneOf`` must advertise
exactly the payload shapes the lifecycle deny-gate enforces.

The MCP catalog (hand-authored in :mod:`specsmither.mcp.server`) and the deny-gate
(:mod:`specsmither.lifecycle.prechecks.schema_validate`) are independent declarations.
If they drift — the catalog advertising ``epicId`` where the gate wants ``id``, or
``{ticketId,dependsOnId}`` where it wants ``{fromTicketId,toTicketId}`` — an agent that
follows the catalog is denied ``invalid_payload``. These tests couple the two so a drift
fails CI.
"""

from __future__ import annotations

from typing import Any

from specsmither.lifecycle.prechecks.schema_validate import (
    PLANNING_OPERATION_CONTRACT,
    CatalogOperationBranch,
    catalog_branches_from_oneof,
    check_planning_catalog_parity,
)
from specsmither.mcp.server import TOOLS


def _action_oneof() -> list[dict[str, Any]]:
    tool = next(t for t in TOOLS if t.name == "action_planning_session")
    one_of = tool.inputSchema["oneOf"]
    assert isinstance(one_of, list)
    return one_of


def test_mcp_catalog_matches_the_deny_gate() -> None:
    """The shipped catalog is in full parity with the canonical operation contract."""
    branches = catalog_branches_from_oneof(_action_oneof())
    assert check_planning_catalog_parity(branches) == []


def test_catalog_covers_every_deny_gate_operation_exactly_once() -> None:
    branches = catalog_branches_from_oneof(_action_oneof())
    ops = [b.operation for b in branches]
    assert set(ops) == set(PLANNING_OPERATION_CONTRACT)
    assert len(ops) == len(set(ops))  # no duplicate branches


def test_create_dependencies_item_shape_is_advertised() -> None:
    """The array-item required fields ride into the catalog (the 0.1.14 fix)."""
    branch = next(
        b
        for b in catalog_branches_from_oneof(_action_oneof())
        if b.operation == "create_dependencies"
    )
    assert branch.item_field == "dependencies"
    assert set(branch.item_required) == {"fromTicketId", "toTicketId"}


# --- the guard has teeth: a drifted catalog must be flagged ------------------------- #


def test_parity_flags_a_stale_identity_field() -> None:
    drifted = [
        CatalogOperationBranch(operation="update_epic", payload_required=("epicId", "fields"))
    ]
    errors = check_planning_catalog_parity(drifted)
    assert any(e.startswith("update_epic:") for e in errors)


def test_parity_flags_a_stale_dependency_edge_shape() -> None:
    drifted = [
        CatalogOperationBranch(
            operation="create_dependencies",
            payload_required=("dependencies",),
            item_field="dependencies",
            item_required=("ticketId", "dependsOnId"),
        )
    ]
    errors = check_planning_catalog_parity(drifted)
    assert any("create_dependencies.dependencies[]" in e for e in errors)


def test_parity_flags_an_op_unknown_to_the_gate() -> None:
    errors = check_planning_catalog_parity(
        [CatalogOperationBranch(operation="frobnicate", payload_required=())]
    )
    assert any("frobnicate" in e for e in errors)


def test_parity_flags_a_missing_operation() -> None:
    errors = check_planning_catalog_parity([])
    assert any(e == "update_spec: missing from catalog oneOf" for e in errors)
