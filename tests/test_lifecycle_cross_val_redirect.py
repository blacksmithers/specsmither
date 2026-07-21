"""cross_val_file_redirect (MB.10): an ordering-only late update_ticket in
cross_validation redirects to create_dependencies instead of rolling back."""

from __future__ import annotations

from typing import Any

import crucible

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.prechecks.cross_val_file_redirect import cross_val_file_redirect
from specsmither.lifecycle.prechecks.result import Accepted, Denied

_CONFIG: Any = crucible.merge_config(crucible.load_defaults())


def _spec(*, b_depends_on_a: bool = False) -> dict[str, Any]:
    # Ticket B references a file ticket A creates. Without a dep edge B→A, that is a pure
    # consume-ordered (#2) gap. With the edge, no finding fires.
    return {
        "id": "s1",
        "epics": [
            {
                "id": "e1",
                "tickets": [
                    {"id": "A", "filesToBeCreated": ["pkg/x.ts"], "dependencies": []},
                    {
                        "id": "B",
                        "filesToBeReferenced": ["pkg/x.ts"],
                        "dependencies": [{"ticketId": "A"}] if b_depends_on_a else [],
                    },
                ],
            }
        ],
    }


_CV = PlanningPhase.CROSS_VALIDATION


def test_redirects_a_pure_ordering_gap_to_create_dependencies() -> None:
    out = cross_val_file_redirect(
        "update_ticket", _CV,
        {"id": "B", "fields": {"filesToBeReferenced": ["pkg/x.ts"]}},
        _spec(), _CONFIG,
    )
    assert isinstance(out, Denied) and out.code == "use_create_dependencies"
    assert out.context["creator_ticket_id"] == "A"
    assert out.context["path"] == "pkg/x.ts"


def test_no_redirect_when_the_dependency_edge_already_exists() -> None:
    # The ordering gap is already closed → no #2 finding → fall through to the rollback.
    out = cross_val_file_redirect(
        "update_ticket", _CV,
        {"id": "B", "fields": {"filesToBeReferenced": ["pkg/x.ts"]}},
        _spec(b_depends_on_a=True), _CONFIG,
    )
    assert isinstance(out, Accepted)


def test_no_redirect_when_a_body_field_is_touched() -> None:
    # A body/structural field → a real change that must roll back, never redirect.
    out = cross_val_file_redirect(
        "update_ticket", _CV,
        {"id": "B", "fields": {"filesToBeReferenced": ["pkg/x.ts"], "description": "d"}},
        _spec(), _CONFIG,
    )
    assert isinstance(out, Accepted)


def test_no_redirect_outside_cross_validation_or_for_other_ops() -> None:
    payload = {"id": "B", "fields": {"filesToBeReferenced": ["pkg/x.ts"]}}
    assert isinstance(
        cross_val_file_redirect("update_ticket", PlanningPhase.TICKET_EXPANSION, payload, _spec(), _CONFIG),
        Accepted,
    )
    assert isinstance(
        cross_val_file_redirect("update_epic", _CV, payload, _spec(), _CONFIG), Accepted
    )
