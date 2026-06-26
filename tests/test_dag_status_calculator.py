"""Unit tests for the pure ticket-status calculator (``dag.status_calculator``).

These exercise the determinism traps relevant to this module:
- ``active``/``done`` are STICKY (never auto-re-derived);
- ``done`` is the only "satisfied" status (anything else, incl. unknown, blocks);
- empty-dependency promotion (vacuous all-done);
- the TS-faithful ``{status, reason}`` branch order + unsatisfied-dep ordering.
"""

from __future__ import annotations

import pytest

from specsmither.dag.status_calculator import (
    DependencyStatus,
    StatusCalculationResult,
    StatusReason,
    UnsatisfiedDependency,
    calculate_status_with_reason,
    calculate_ticket_status,
    should_recalculate,
)

# --- should_recalculate --------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "ready"])
def test_should_recalculate_true_for_recalculable(status: str) -> None:
    assert should_recalculate(status) is True


@pytest.mark.parametrize("status", ["active", "done", "", "blocked", "PENDING"])
def test_should_recalculate_false_otherwise(status: str) -> None:
    # active/done are sticky; unknown/uppercase strings are not recalculable.
    assert should_recalculate(status) is False


# --- calculate_ticket_status: sticky active/done -------------------------------


@pytest.mark.parametrize("status", ["active", "done"])
def test_active_done_sticky_with_unmet_deps(status: str) -> None:
    # Even with an unmet dependency, sticky statuses are returned unchanged.
    assert calculate_ticket_status(status, ["pending"]) == status


@pytest.mark.parametrize("status", ["active", "done"])
def test_active_done_sticky_even_when_all_deps_done(status: str) -> None:
    # All deps done must NOT flip a sticky status to "ready".
    assert calculate_ticket_status(status, ["done", "done"]) == status


def test_unknown_status_returned_unchanged() -> None:
    assert calculate_ticket_status("weird", ["done"]) == "weird"


# --- calculate_ticket_status: pending --------------------------------------------


def test_pending_promotes_to_ready_when_all_deps_done() -> None:
    assert calculate_ticket_status("pending", ["done", "done", "done"]) == "ready"


def test_pending_stays_pending_with_unmet_dep() -> None:
    assert calculate_ticket_status("pending", ["done", "active"]) == "pending"


def test_pending_stays_pending_with_unknown_dep() -> None:
    # An unknown/missing dependency is surfaced as a non-"done" status string
    # and must block promotion (NOT-EXISTS-non-done predicate).
    assert calculate_ticket_status("pending", ["done", "unknown"]) == "pending"
    assert calculate_ticket_status("pending", [""]) == "pending"


# --- calculate_ticket_status: ready ----------------------------------------------


def test_ready_reverts_to_pending_when_a_dep_reverts() -> None:
    assert calculate_ticket_status("ready", ["done", "pending"]) == "pending"


def test_ready_stays_ready_when_all_deps_done() -> None:
    assert calculate_ticket_status("ready", ["done"]) == "ready"


# --- calculate_ticket_status: empty deps -----------------------------------------


def test_empty_deps_promotes_pending_to_ready() -> None:
    assert calculate_ticket_status("pending", []) == "ready"


def test_empty_deps_keeps_ready_ready() -> None:
    assert calculate_ticket_status("ready", []) == "ready"


def test_only_done_string_counts_as_satisfied() -> None:
    # Statuses other than the literal "done" never satisfy.
    assert calculate_ticket_status("pending", ["DONE"]) == "pending"
    assert calculate_ticket_status("pending", ["completed"]) == "pending"


# --- calculate_status_with_reason: TS-faithful branches --------------------------


def test_reason_external_block_forces_pending() -> None:
    # block_reason wins even though the only dependency is done.
    result = calculate_status_with_reason(
        "Blocked by upstream",
        [DependencyStatus(id="a", title="A", status="done")],
    )
    assert result == StatusCalculationResult(
        status="pending",
        reason=StatusReason(type="external_block", block_reason="Blocked by upstream"),
    )


def test_reason_empty_block_reason_is_not_a_block() -> None:
    # Empty string is falsy in the TS truthiness check — treated as no block.
    result = calculate_status_with_reason("", [])
    assert result.status == "ready"
    assert result.reason.type == "no_dependencies"


def test_reason_no_dependencies() -> None:
    result = calculate_status_with_reason(None, [])
    assert result.status == "ready"
    assert result.reason.type == "no_dependencies"
    assert result.reason.unsatisfied_deps is None


def test_reason_dependencies_satisfied() -> None:
    result = calculate_status_with_reason(
        None,
        [
            DependencyStatus(id="a", title="A", status="done"),
            DependencyStatus(id="b", title="B", status="done"),
        ],
    )
    assert result.status == "ready"
    assert result.reason.type == "dependencies_satisfied"


def test_reason_dependencies_unsatisfied_preserves_order() -> None:
    result = calculate_status_with_reason(
        None,
        [
            DependencyStatus(id="a", title="A", status="done"),
            DependencyStatus(id="b", title="B", status="active"),
            DependencyStatus(id="c", title="C", status="pending"),
        ],
    )
    assert result.status == "pending"
    assert result.reason.type == "dependencies_unsatisfied"
    assert result.reason.unsatisfied_deps == (
        UnsatisfiedDependency(id="b", title="B", status="active"),
        UnsatisfiedDependency(id="c", title="C", status="pending"),
    )
