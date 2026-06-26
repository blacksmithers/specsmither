"""Unit tests for the ticket status machine (``specsmither.domain.status``)."""

from __future__ import annotations

import pytest

from specsmither.domain.enums import TicketStatus
from specsmither.domain.status import (
    ACTIONABLE_STATUSES,
    ACTIVE_STATUSES,
    BLOCKED_STATUSES,
    COMPLETE_STATUSES,
    TICKET_STATUSES,
    VALID_TRANSITIONS,
    get_valid_target_statuses,
    is_actionable_status,
    is_active_status,
    is_blocked_status,
    is_complete_status,
    is_valid_status,
    is_valid_transition,
)


def test_ticket_statuses_tuple() -> None:
    assert TICKET_STATUSES == (
        TicketStatus.PENDING,
        TicketStatus.READY,
        TicketStatus.ACTIVE,
        TicketStatus.DONE,
    )


def test_category_tuples() -> None:
    assert ACTIONABLE_STATUSES == (TicketStatus.READY,)
    assert BLOCKED_STATUSES == (TicketStatus.PENDING,)
    assert ACTIVE_STATUSES == (TicketStatus.ACTIVE,)
    assert COMPLETE_STATUSES == (TicketStatus.DONE,)


def test_valid_transitions_map_exact() -> None:
    assert VALID_TRANSITIONS == {
        TicketStatus.PENDING: (TicketStatus.READY, TicketStatus.PENDING),
        TicketStatus.READY: (TicketStatus.ACTIVE, TicketStatus.PENDING),
        TicketStatus.ACTIVE: (TicketStatus.DONE, TicketStatus.PENDING, TicketStatus.READY),
        TicketStatus.DONE: (TicketStatus.ACTIVE, TicketStatus.PENDING, TicketStatus.READY),
    }


@pytest.mark.parametrize("value", ["pending", "ready", "active", "done"])
def test_is_valid_status_true(value: str) -> None:
    assert is_valid_status(value) is True


@pytest.mark.parametrize("value", ["", "blocked", "PENDING", "garbage", "todo"])
def test_is_valid_status_false(value: str) -> None:
    assert is_valid_status(value) is False


def test_every_valid_transition_edge_true() -> None:
    for source, targets in VALID_TRANSITIONS.items():
        for target in targets:
            assert is_valid_transition(source.value, target.value) is True


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("pending", "active"),
        ("pending", "done"),
        ("ready", "done"),
        ("ready", "ready"),
        ("active", "active"),
        ("done", "done"),
    ],
)
def test_invalid_transition_edges_false(source: str, target: str) -> None:
    assert is_valid_transition(source, target) is False


def test_transition_coercion_fallback_behaves_as_pending() -> None:
    # 'garbage' coerces to PENDING; PENDING -> ready is valid.
    assert is_valid_transition("garbage", "ready") is True
    assert is_valid_transition("pending", "ready") is True
    # PENDING -> active is invalid, so garbage -> active is invalid too.
    assert is_valid_transition("garbage", "active") is False
    # Unknown target also coerces to PENDING; PENDING -> pending is valid.
    assert is_valid_transition("ready", "garbage") is True


def test_get_valid_target_statuses() -> None:
    assert get_valid_target_statuses("active") == (
        TicketStatus.DONE,
        TicketStatus.PENDING,
        TicketStatus.READY,
    )
    # Unknown coerces to PENDING.
    assert get_valid_target_statuses("garbage") == (TicketStatus.READY, TicketStatus.PENDING)


def test_predicate_actionable() -> None:
    assert is_actionable_status("ready") is True
    assert is_actionable_status("pending") is False
    assert is_actionable_status("garbage") is False  # coerces to PENDING


def test_predicate_blocked() -> None:
    assert is_blocked_status("pending") is True
    assert is_blocked_status("ready") is False
    # Unknown coerces to PENDING, which IS the blocked status.
    assert is_blocked_status("garbage") is True


def test_predicate_active() -> None:
    assert is_active_status("active") is True
    assert is_active_status("done") is False
    assert is_active_status("garbage") is False


def test_predicate_complete() -> None:
    assert is_complete_status("done") is True
    assert is_complete_status("active") is False
    assert is_complete_status("garbage") is False
