"""Creator-election guidance — the CPS shared-file gate_not_passed deny (MB.13).

Covers the lifecycle-owned rendering over the structural analyzer: the two-pass
batch-it framing, the per-file election + move + deps, the clean/conflict tag, and
that the plan is surfaced only at cross_validation on a file-provenance deny.
"""

from __future__ import annotations

from typing import Any

from crucible.cross_validation.creator_election import elect_file_creators

from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.guidance.creator_plan_guidance import format_creator_plan
from specsmither.lifecycle.ports import (
    SpecFull,
    ValidatorFinding,
    ValidatorOutput,
)
from specsmither.lifecycle.prechecks import Accepted, Denied, gate_currently_passing


def _orphan_spec(**ticket_extra: Any) -> dict[str, Any]:
    """Two tickets both modifying a file no ticket creates → one clean orphan plan."""
    t1: dict[str, Any] = {"id": "t1", "epicId": "e1", "order": 0, "filesToBeModified": ["s.py"]}
    t1.update(ticket_extra)
    return {
        "epics": [
            {
                "id": "e1",
                "order": 0,
                "tickets": [
                    t1,
                    {"id": "t2", "epicId": "e1", "order": 1, "filesToBeModified": ["s.py"]},
                ],
            }
        ]
    }


# --------------------------------------------------------------------------- #
# format_creator_plan rendering                                                #
# --------------------------------------------------------------------------- #


def test_empty_plan_renders_nothing() -> None:
    # A spec whose only file is created by a ticket has no orphan.
    spec = {"epics": [{"id": "e1", "order": 0, "tickets": [
        {"id": "t1", "epicId": "e1", "order": 0, "filesToBeCreated": ["s.py"]},
    ]}]}
    assert format_creator_plan(elect_file_creators(spec), "en") == ""


def test_clean_plan_renders_election_move_and_deps() -> None:
    block = format_creator_plan(elect_file_creators(_orphan_spec()), "en")
    assert "1 shared file is touched by planning tickets but created by none" in block
    assert '• "s.py" — touched by 2 tickets, created by none.' in block
    assert "elect t1 as the creator" in block
    assert '`update_ticket t1` — move "s.py" from filesToBeModified → filesToBeCreated' in block
    assert "`create_dependencies` — t2 requires t1." in block
    assert "✓ acyclic" in block


def test_conflict_plan_degrades_to_get_ticket_orientation() -> None:
    # t1 (the earliest toucher / natural creator) already requires t2 → electing t1 and
    # declaring t2 requires t1 would close a cycle.
    spec = _orphan_spec(dependencies=[{"ticketId": "t2"}])
    block = format_creator_plan(elect_file_creators(spec), "en")
    assert "⚠ conflict — do NOT auto-elect" in block
    assert "`get_ticket t1 t2`" in block
    assert "✓ acyclic" not in block


def test_creator_plan_renders_in_pt_br() -> None:
    block = format_creator_plan(elect_file_creators(_orphan_spec()), "pt-br")
    assert "arquivo compartilhado é tocado por tickets de planejamento" in block
    assert "eleja t1 como o criador" in block
    assert "t2 requer t1." in block
    assert "✓ acíclico" in block


# --------------------------------------------------------------------------- #
# CPS gate integration: surfaced only on a cross_validation file-provenance deny #
# --------------------------------------------------------------------------- #


class _FakeValidator:
    def __init__(self, output: ValidatorOutput) -> None:
        self._output = output

    def validate(self, spec_full: Any, phase: Any, config: Any, language: str = "en") -> ValidatorOutput:
        return self._output


def _spec_full_with_orphan() -> SpecFull:
    from crucible.models.enums import Complexity, TicketType

    from specsmither.domain.enums import (
        EpicStatus,
        SpecStatus,
        TicketStatus,
    )
    from specsmither.domain.records import (
        EpicRecord,
        SpecificationRecord,
        TicketRecord,
        build_spec_full,
    )

    spec = SpecificationRecord(id="spec-1", project_id="proj-1", title="T", status=SpecStatus.PLANNING)
    epic = EpicRecord(
        id="e1",
        specification_id="spec-1",
        title="E1",
        description="d",
        objective="o",
        order=0,
        status=EpicStatus.TODO,
    )

    def _ticket(tid: str, order: int) -> TicketRecord:
        return TicketRecord(
            id=tid,
            epic_id="e1",
            title=tid,
            order=order,
            ticket_type=TicketType.IMPLEMENTATION,
            complexity=Complexity.SMALL,
            estimated_minutes=30,
            status=TicketStatus.PENDING,
            files_to_be_modified=["s.py"],
        )

    nested = build_spec_full(spec, [epic], [_ticket("t1", 0), _ticket("t2", 1)], [], [])
    return SpecFull(spec=nested, epics=[], blueprints=[], dependencies=[])


def _fail_output(findings: list[ValidatorFinding]) -> ValidatorOutput:
    return ValidatorOutput(
        gate_result="fail",
        local_score=0.0,
        per_epic_score={},
        per_ticket_score={},
        findings=findings,
        validated_phase=PlanningPhase.CROSS_VALIDATION,
    )


def _finding(path: str) -> ValidatorFinding:
    return ValidatorFinding(
        category="cross-validation",  # type: ignore[arg-type]
        message="a file-provenance blocker",
        severity="finding",
        entity_id=None,
        entity_type=None,
        path=path,
        points_lost=0.0,
        global_impact_on_fix=0.0,
    )


def _session() -> Any:
    from specsmither.db.models import PlanningSession
    from specsmither.domain.enums import PlanningSessionStatus

    return PlanningSession(
        id="ps-1",
        specification_id="spec-1",
        status=PlanningSessionStatus.ACTIVE.value,
        current_phase=PlanningPhase.CROSS_VALIDATION.value,
    )


def test_plan_prepended_on_cross_validation_file_provenance_deny() -> None:
    output = _fail_output([_finding("/cross-validation/file-provenance")])
    result = gate_currently_passing(
        _session(), _spec_full_with_orphan(), _FakeValidator(output), {"thresholds": {}}
    )
    assert isinstance(result, Denied) and result.code == "gate_not_passed"
    assert result.blockers is not None
    # The plan leads the blockers, above the flat finding message.
    assert result.blockers[0].startswith("1 shared file is touched by planning tickets")
    assert "a file-provenance blocker" in result.blockers[-1]


def test_plan_uses_the_grep_evidence_the_gate_used() -> None:
    # The shared file is real in the repo (grep evidence) → NOT an orphan → no plan,
    # even though the gate still carries a (different) file-provenance finding.
    output = _fail_output([_finding("/cross-validation/file-provenance")])
    output = ValidatorOutput(
        gate_result=output.gate_result,
        local_score=output.local_score,
        per_epic_score=output.per_epic_score,
        per_ticket_score=output.per_ticket_score,
        findings=output.findings,
        validated_phase=output.validated_phase,
        existing_files=frozenset({"s.py"}),
    )
    result = gate_currently_passing(
        _session(), _spec_full_with_orphan(), _FakeValidator(output), {"thresholds": {}}
    )
    assert isinstance(result, Denied)
    assert result.blockers is not None
    assert not any("shared file" in b for b in result.blockers)


def test_no_plan_without_a_file_provenance_finding() -> None:
    # Gate fails for an unrelated reason → no orphan plan is surfaced.
    output = _fail_output([_finding("/cross-validation/wave-assignment")])
    result = gate_currently_passing(
        _session(), _spec_full_with_orphan(), _FakeValidator(output), {"thresholds": {}}
    )
    assert isinstance(result, Denied) and result.code == "gate_not_passed"
    assert result.blockers is not None
    assert not any("shared file" in b for b in result.blockers)


def test_pass_gate_accepts_without_a_plan() -> None:
    output = ValidatorOutput(
        gate_result="pass",
        local_score=1.0,
        per_epic_score={},
        per_ticket_score={},
        findings=[],
        validated_phase=PlanningPhase.CROSS_VALIDATION,
    )
    result = gate_currently_passing(
        _session(), _spec_full_with_orphan(), _FakeValidator(output), {"thresholds": {}}
    )
    assert isinstance(result, Accepted)
