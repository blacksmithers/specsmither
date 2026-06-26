"""L7 acceptance: loose-reference lookup (#22) over a real on-disk SQLite database.

Three guarantees from the work-item contract:

1. **Exact-number** resolves the right epic / ticket by ``epic_number`` /
   ``ticket_number`` (both as an ``int`` and as an all-digit string).
2. **Fuzzy substring** resolves the right project / specification / epic / ticket
   by a case-insensitive ``contains`` over the name/title.
3. **The deterministic tiebreak** — two entities matching a query equally always
   resolve to the SAME one across repeated calls. The fixtures are seeded so that
   row-id / insertion order is the *reverse* of the pinned number order, proving
   the ``ORDER BY`` is pinned by number (lower number wins), not by insertion.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.models import Epic, Project, Specification, Ticket
from specsmither.operations.lookup import (
    lookup_epic,
    lookup_project,
    lookup_specification,
    lookup_ticket,
)

PROJECT_ID = "01PROJECT0000000000000000A"
SPEC_ID = "01SPEC000000000000000000A"
SPEC_BILLING_ID = "01SPEC000000000000000000B"
# Auth epics: id order (A < B) is the REVERSE of number order (B is number 1).
EPIC_AUTH_A_ID = "01EPICAUTHA00000000000000"  # epic_number 2, "Auth service"
EPIC_AUTH_B_ID = "01EPICAUTHB00000000000000"  # epic_number 1, "Auth gateway"
# Login tickets (both under EPIC_AUTH_B): id order (A < B) reverses number order.
TICKET_LOGIN_A_ID = "01TICKETLOGINA00000000000"  # ticket_number 2, "Login page"
TICKET_LOGIN_B_ID = "01TICKETLOGINB00000000000"  # ticket_number 1, "Login form"


def _open(tmp_path: Path) -> sessionmaker[Session]:
    engine = init_db(tmp_path / "lookup.db")
    return make_session_factory(engine)


def _seed(session: Session) -> None:
    session.add(Project(id=PROJECT_ID, name="Acme Platform"))
    # Two specs in the project; created order pins the fuzzy tiebreak.
    session.add(
        Specification(
            id=SPEC_ID,
            project_id=PROJECT_ID,
            title="Onboarding flow",
            specification_type_id=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    session.add(
        Specification(
            id=SPEC_BILLING_ID,
            project_id=PROJECT_ID,
            title="Billing system",
            specification_type_id=None,
            created_at="2026-01-02T00:00:00+00:00",
        )
    )
    # Two epics whose titles both contain "auth". Inserted lower-id FIRST but with
    # the HIGHER number, so a by-id/insertion order would pick the wrong one.
    session.add(
        Epic(
            id=EPIC_AUTH_A_ID,
            specification_id=SPEC_ID,
            epic_number=2,
            title="Auth service",
            description="d",
            objective="o",
            order=1,
        )
    )
    session.add(
        Epic(
            id=EPIC_AUTH_B_ID,
            specification_id=SPEC_ID,
            epic_number=1,
            title="Auth gateway",
            description="d",
            objective="o",
            order=2,
        )
    )
    session.flush()  # land the FK parents before their ticket children.
    # Two tickets under EPIC_AUTH_B whose titles both contain "login"; again id
    # order reverses number order.
    session.add(
        Ticket(
            id=TICKET_LOGIN_A_ID,
            epic_id=EPIC_AUTH_B_ID,
            title="Login page",
            ticket_number=2,
            estimated_minutes=10,
        )
    )
    session.add(
        Ticket(
            id=TICKET_LOGIN_B_ID,
            epic_id=EPIC_AUTH_B_ID,
            title="Login form",
            ticket_number=1,
            estimated_minutes=10,
        )
    )


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    factory = _open(tmp_path)
    with factory.begin() as session:
        _seed(session)
    return factory


# --------------------------------------------------------------------------- #
# 1. exact-number lookup                                                       #
# --------------------------------------------------------------------------- #


def test_exact_number_resolves_epic(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    # int reference ...
    epic = lookup_epic(factory, SPEC_ID, 2)
    assert epic is not None
    assert epic.id == EPIC_AUTH_A_ID
    assert epic.epic_number == 2
    # ... and the equivalent all-digit string reference.
    epic_str = lookup_epic(factory, SPEC_ID, "1")
    assert epic_str is not None
    assert epic_str.id == EPIC_AUTH_B_ID
    assert epic_str.epic_number == 1


def test_exact_number_resolves_ticket(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    ticket = lookup_ticket(factory, EPIC_AUTH_B_ID, 2)
    assert ticket is not None
    assert ticket.id == TICKET_LOGIN_A_ID
    assert ticket.ticket_number == 2

    ticket_str = lookup_ticket(factory, EPIC_AUTH_B_ID, "1")
    assert ticket_str is not None
    assert ticket_str.id == TICKET_LOGIN_B_ID


# --------------------------------------------------------------------------- #
# 2. fuzzy substring lookup                                                    #
# --------------------------------------------------------------------------- #


def test_fuzzy_substring_resolves(tmp_path: Path) -> None:
    factory = _factory(tmp_path)

    project = lookup_project(factory, "acme")  # case-insensitive
    assert project is not None
    assert project.id == PROJECT_ID

    spec = lookup_specification(factory, PROJECT_ID, "boarding")  # mid-word substring
    assert spec is not None
    assert spec.id == SPEC_ID

    other = lookup_specification(factory, PROJECT_ID, "BILL")
    assert other is not None
    assert other.id == SPEC_BILLING_ID

    epic = lookup_epic(factory, SPEC_ID, "gateway")
    assert epic is not None
    assert epic.id == EPIC_AUTH_B_ID

    ticket = lookup_ticket(factory, EPIC_AUTH_B_ID, "form")
    assert ticket is not None
    assert ticket.id == TICKET_LOGIN_B_ID


def test_unresolvable_reference_returns_none(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    assert lookup_project(factory, "nonexistent") is None
    assert lookup_specification(factory, PROJECT_ID, "zzz") is None
    assert lookup_epic(factory, SPEC_ID, "zzz") is None
    assert lookup_ticket(factory, EPIC_AUTH_B_ID, "zzz") is None
    # An empty reference resolves nothing.
    assert lookup_specification(factory, PROJECT_ID, "") is None
    # A number with no matching epic falls through to the (non-matching) substring.
    assert lookup_epic(factory, SPEC_ID, 99) is None


# --------------------------------------------------------------------------- #
# 3. the deterministic tiebreak                                               #
# --------------------------------------------------------------------------- #


def test_deterministic_tiebreak_epic_lower_number_wins(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    # Both "Auth service" (#2, lower id) and "Auth gateway" (#1, higher id) match
    # "auth". The pinned ORDER BY (number, then id) must pick #1 — and the SAME one
    # on every call, never the lower-id / first-inserted #2.
    results = [lookup_epic(factory, SPEC_ID, "auth") for _ in range(5)]
    assert all(r is not None for r in results)
    ids = {r.id for r in results if r is not None}
    assert ids == {EPIC_AUTH_B_ID}  # always the lower epic_number (1), not the lower id
    numbers = {r.epic_number for r in results if r is not None}
    assert numbers == {1}


def test_deterministic_tiebreak_ticket_lower_number_wins(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    # "Login page" (#2, lower id) and "Login form" (#1, higher id) both match "login".
    results = [lookup_ticket(factory, EPIC_AUTH_B_ID, "login") for _ in range(5)]
    assert all(r is not None for r in results)
    ids = {r.id for r in results if r is not None}
    assert ids == {TICKET_LOGIN_B_ID}  # always ticket_number 1, not the lower id
    numbers = {r.ticket_number for r in results if r is not None}
    assert numbers == {1}
