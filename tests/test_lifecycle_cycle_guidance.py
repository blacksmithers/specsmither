"""Cycle-resolution guidance — the enriched ``cycle_detected`` deny (MB.12).

Covers the lifecycle-owned rendering over the structural analyzer: the
intra-batch-vs-persisted origin tag, the 2-D recovery dispatch (cycle shape × the
spurious edge's origin), and that the whole packet renders in the configured
language.
"""

from __future__ import annotations

from typing import Any

from crucible.cross_validation.cycle_analysis import StructuralCycleEdge

from specsmither.lifecycle.ports import SpecDependencyEdge
from specsmither.lifecycle.prechecks.dependencies_batch import (
    BatchValidationCycle,
    DetectedCycle,
)
from specsmither.lifecycle.verbs.cycle_guidance import (
    build_cycle_detected_denial,
    tag_cycle_edge_origin,
)


def _edge(a: str, b: str) -> SpecDependencyEdge:
    return SpecDependencyEdge(from_ticket_id=a, to_ticket_id=b)


# A 2-cycle t1 ⇄ t2 where t2 consumes a file t1 creates → t2→t1 is file-backed
# (keep), t1→t2 is the unbacked, order-reverse (spurious) edge.
_SPEC: dict[str, Any] = {
    "epics": [
        {
            "id": "e1",
            "order": 0,
            "tickets": [
                {"id": "t1", "epicId": "e1", "order": 0, "filesToBeCreated": ["a.py"]},
                {"id": "t2", "epicId": "e1", "order": 1, "filesToBeModified": ["a.py"]},
            ],
        }
    ]
}


def _cycle() -> BatchValidationCycle:
    path = [_edge("t1", "t2"), _edge("t2", "t1")]
    return BatchValidationCycle(
        cycles=[DetectedCycle(new_edge=_edge("t1", "t2"), cycle_path=path)],
        dropped_intra_batch=[],
        dropped_persisted=[],
    )


def test_tag_cycle_edge_origin_marks_persisted_vs_intra_batch() -> None:
    path = [
        StructuralCycleEdge(from_ticket_id="t1", to_ticket_id="t2"),
        StructuralCycleEdge(from_ticket_id="t2", to_ticket_id="t1"),
    ]
    origins = tag_cycle_edge_origin(path, existing=[_edge("t2", "t1")])
    assert origins == ["intra-batch", "persisted"]


def test_intra_batch_spurious_edge_recovers_by_resubmit_omitting() -> None:
    denial = build_cycle_detected_denial(_cycle(), _SPEC, existing=[], language="en")
    assert denial.code == "cycle_detected"
    # The evidence identifies t1 → t2 as spurious (unbacked + order-reverse); nothing is
    # persisted, so the recovery is RESUBMIT-omitting, never delete_dependencies.
    assert "RESUBMIT `create_dependencies` OMITTING the spurious edge `t1 → t2`" in denial.message
    assert "`get_ticket t1 t2`" in denial.message
    assert "one direction is file-backed" in denial.message
    # The raw node sequence is kept for the audit detail bag, not the prose.
    assert denial.context is not None
    assert denial.context["cycle_paths"] == ["t1 -> t2 -> t1"]


def test_persisted_spurious_edge_recovers_by_delete_dependencies() -> None:
    # The spurious edge t1 → t2 is already in the persisted graph → delete it.
    denial = build_cycle_detected_denial(
        _cycle(), _SPEC, existing=[_edge("t1", "t2")], language="en"
    )
    assert "`delete_dependencies` the spurious edge `t1 → t2`" in denial.message
    assert "already persisted" in denial.message


def test_all_file_backed_cycle_recovers_by_reshaping_files() -> None:
    # Both directions consume a file the other creates → contradictory FILE dependency.
    spec: dict[str, Any] = {
        "epics": [
            {
                "id": "e1",
                "order": 0,
                "tickets": [
                    {
                        "id": "t1",
                        "epicId": "e1",
                        "order": 0,
                        "filesToBeCreated": ["a.py"],
                        "filesToBeModified": ["b.py"],
                    },
                    {
                        "id": "t2",
                        "epicId": "e1",
                        "order": 1,
                        "filesToBeCreated": ["b.py"],
                        "filesToBeModified": ["a.py"],
                    },
                ],
            }
        ]
    }
    denial = build_cycle_detected_denial(_cycle(), spec, existing=[], language="en")
    assert "reshape the file assignments with `update_ticket`" in denial.message
    assert "a circular FILE dependency" in denial.message


def test_cycle_guidance_renders_in_pt_br() -> None:
    denial = build_cycle_detected_denial(_cycle(), _SPEC, existing=[], language="pt-br")
    assert "Ciclo 1:" in denial.message
    assert "RE-ENVIE `create_dependencies` OMITINDO a aresta espúria `t1 → t2`" in denial.message
    assert "uma direção é respaldada por arquivo" in denial.message
    # Operation names stay canonical even in pt-br prose.
    assert "`get_ticket t1 t2`" in denial.message
