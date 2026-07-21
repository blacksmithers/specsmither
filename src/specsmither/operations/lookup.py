"""Loose-reference entity resolution.

The ``lookup_*`` verbs resolve a *loose* user reference — the kind a human types
at a prompt — to a single concrete entity. Two resolution modes, in precedence
order:

1. **Exact number** — when the reference denotes an integer (an ``int``, or an
   all-digit string), match it against the entity's user-facing number
   (``epic_number`` / ``ticket_number``). Projects and specifications carry no
   number, so this mode never applies to them.
2. **Fuzzy substring** — case-insensitive ``needle in title`` (``name`` for a
   project). The needle is the trimmed, lower-cased string form of the reference.

DETERMINISM (the load-bearing contract). A naive first-match returns whatever
element the store happened to iterate first — fragile. Here every candidate set is
sorted under an **explicit, pinned key** before the first match is taken, so two
equally-good matches always resolve to the *same* one:

* epics / tickets — by ascending ``epic_number`` / ``ticket_number`` (un-numbered
  rows last), then ``id``. The lower number always wins; ``id`` is the final,
  unique tiebreak. This is independent of the store's display-``order`` ``ORDER BY``
  and of row-insertion order.
* projects / specifications — by ``created_at`` (creation order), then ``id``.

These are pure read-only verbs: each opens a short-lived read session
(``with session_factory() as session``), reads through the stores, and returns a
detached :mod:`~specsmither.domain.records` DTO. They never recompute and never
write (architecture invariant 3 governs *mutations* only).

Scope: there is no slow-query telemetry wrapper and no access-control layer
(single local user). A batch multi-get gated by per-id access is not a fuzzy
resolver, so it is out of this module's scope. An unresolvable reference returns
``None`` rather than raising — validation/guidance is a 0.1.0 dispatch concern; a
``lookup`` is best-effort by contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from specsmither.db.repositories import make_stores

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.db.repositories import ProjectRecord
    from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord

__all__ = [
    "LookupRef",
    "lookup_epic",
    "lookup_project",
    "lookup_specification",
    "lookup_ticket",
]

#: A loose user reference: an integer (number lookup) or free text (fuzzy lookup).
LookupRef = str | int

_R = TypeVar("_R")


# --------------------------------------------------------------------------- #
# reference parsing                                                            #
# --------------------------------------------------------------------------- #


def _coerce_number(ref: LookupRef) -> int | None:
    """Return the integer *ref* denotes (an ``int`` or an all-digit string), else ``None``.

    A free-text reference (``"auth"``, ``"3.5"``, ``""``) has no exact number to
    match and yields ``None`` — the caller then falls through to fuzzy substring.
    """
    if isinstance(ref, int):
        return ref
    text = ref.strip()
    body = text[1:] if text[:1] in {"+", "-"} else text
    return int(text) if body.isdigit() else None


def _needle(ref: LookupRef) -> str:
    """The trimmed, lower-cased substring needle for the fuzzy match."""
    return str(ref).strip().lower()


# --------------------------------------------------------------------------- #
# deterministic ordering keys                                                  #
# --------------------------------------------------------------------------- #


def _number_key(number: int | None, entity_id: str) -> tuple[bool, int, str]:
    """Pinned order for numbered entities: ascending number (un-numbered last), then id."""
    return (number is None, number if number is not None else 0, entity_id)


def _created_key(created_at: str | None, entity_id: str) -> tuple[str, str]:
    """Pinned order for un-numbered entities: creation order, then id."""
    return (created_at or "", entity_id)


# --------------------------------------------------------------------------- #
# the resolution core (number-first, then fuzzy substring)                     #
# --------------------------------------------------------------------------- #


def _best_match(
    ordered: Sequence[_R],
    *,
    needle: str,
    get_text: Callable[[_R], str],
    number: int | None = None,
    get_number: Callable[[_R], int | None] | None = None,
) -> _R | None:
    """First match over an already-pinned-ordered sequence: exact number, then substring.

    ``ordered`` MUST already be sorted under the deterministic key — this routine
    only does the first-match scan, so the "which equal match wins" decision is
    fully owned by the caller's pinned ``sorted(...)``.
    """
    if number is not None and get_number is not None:
        for record in ordered:
            if get_number(record) == number:
                return record
    if needle:
        for record in ordered:
            if needle in get_text(record).lower():
                return record
    return None


# --------------------------------------------------------------------------- #
# public verbs                                                                 #
# --------------------------------------------------------------------------- #


def lookup_project(
    session_factory: sessionmaker[Session], ref: LookupRef
) -> ProjectRecord | None:
    """Resolve a project by fuzzy substring over ``name`` (no number to match)."""
    needle = _needle(ref)
    if not needle:
        return None
    with session_factory() as session:
        projects = make_stores(session).projects.list_projects()
    ordered = sorted(projects, key=lambda p: _created_key(p.created_at, p.id))
    return _best_match(ordered, needle=needle, get_text=lambda p: p.name)


def lookup_specification(
    session_factory: sessionmaker[Session], project_id: str, ref: LookupRef
) -> SpecificationRecord | None:
    """Resolve a specification within ``project_id`` by fuzzy substring over ``title``."""
    needle = _needle(ref)
    if not needle:
        return None
    with session_factory() as session:
        specs = make_stores(session).specifications.list_specifications(project_id=project_id)
    ordered = sorted(specs, key=lambda s: _created_key(s.created_at, s.id))
    return _best_match(ordered, needle=needle, get_text=lambda s: s.title)


def lookup_epic(
    session_factory: sessionmaker[Session], specification_id: str, ref: LookupRef
) -> EpicRecord | None:
    """Resolve an epic within ``specification_id`` — exact ``epic_number`` then title."""
    needle = _needle(ref)
    number = _coerce_number(ref)
    if not needle and number is None:
        return None
    with session_factory() as session:
        epics = make_stores(session).epics.list_epics(specification_id=specification_id)
    ordered = sorted(epics, key=lambda e: _number_key(e.epic_number, e.id))
    return _best_match(
        ordered,
        number=number,
        get_number=lambda e: e.epic_number,
        needle=needle,
        get_text=lambda e: e.title,
    )


def lookup_ticket(
    session_factory: sessionmaker[Session], epic_id: str, ref: LookupRef
) -> TicketRecord | None:
    """Resolve a ticket within ``epic_id`` — exact ``ticket_number`` then title."""
    needle = _needle(ref)
    number = _coerce_number(ref)
    if not needle and number is None:
        return None
    with session_factory() as session:
        tickets = make_stores(session).tickets.list_tickets(epic_id=epic_id)
    ordered = sorted(tickets, key=lambda t: _number_key(t.ticket_number, t.id))
    return _best_match(
        ordered,
        number=number,
        get_number=lambda t: t.ticket_number,
        needle=needle,
        get_text=lambda t: t.title,
    )
