"""Unified ticket search — work item #20 (port of ``operations/search.ts``).

SpecSmither keeps the TS ``searchTickets`` behaviour **verbatim** but runs it as a
load-scope-then-score-in-Python pipeline (decision 3: NO authoritative FTS5). The
seam against the TS is purely the scope load (DynamoDB stores → the SQLite
``*StoreSqlite`` repositories) and the dropped multi-user auth (``userId`` /
``requireProjectAccess`` / ``sharingStore`` disappear with the single local user).
Everything else — substring matching, glob→regex, tag AND/OR, ``relatedTo``
similarity, the additive relevance weights, the sort and the pagination — is the
same logic, reproduced so a differential test against the TS passes.

Why substring, not full-text: the TS matches with JS ``String.includes`` (a
case-insensitive **substring** test), not tokenised/prefix matching. FTS5 would
silently drop matches like ``"thenti"`` inside ``"authentication"``; a SQL
``LOWER(col) LIKE '%?%'`` prefilter or a Python ``in`` test is the only faithful
option. We do the membership test (and all scoring) in Python.

Two entry points:

* :func:`search_tickets` — the DB entry. Opens a **short-lived read session**
  (``with session_factory() as s`` — a read, so it never recomputes; invariant 3
  reserves recompute for the CRUD mutations), resolves the
  ``epicId`` / ``specificationId`` / ``projectId`` scope to a flat list of
  :class:`SearchTicket`, then runs the pure pipeline.
* :func:`run_search` — the pure pipeline over an in-memory ``Sequence`` of
  :class:`SearchTicket` (no DB), exposed so the scoring/glob/tag/sort behaviour is
  testable as a golden unit and so a future caller can reuse it.

The MCP ``search`` composer + dispatch facade that wrap this are 0.1.0 — out of
scope here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from specsmither.db.repositories import make_stores
from specsmither.operations.errors import NotFoundError, ValidationFailedError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.db.repositories import AllStores
    from specsmither.domain.records import TicketRecord

__all__ = [
    "SearchFilters",
    "SearchResult",
    "SearchTicket",
    "SearchTicketItem",
    "match_glob",
    "run_search",
    "search_tickets",
]

# Default pagination window (TS: `limit = 20`, `offset = 0`).
_DEFAULT_LIMIT = 20


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchTicket:
    """A ticket flattened into the searchable surface (TS ``TicketInScope``).

    ``files`` is the **union of all four planned file-change kinds**
    (created / modified / deleted / referenced) — "which tickets touch this path",
    regardless of how — mirroring the TS, which hydrates ``files`` from the union of
    the ``TicketFileChange`` hasMany. ``status`` / ``complexity`` are the plain
    string values (``complexity`` absent ⇒ ``None``, treated as ``""`` by the filter).
    """

    id: str
    epic_id: str
    title: str
    status: str
    ticket_number: int | None = None
    description: str | None = None
    complexity: str | None = None
    tags: Sequence[str] = ()
    files: Sequence[str] = ()


@dataclass(frozen=True)
class SearchTicketItem:
    """One scored hit (TS ``SearchTicketItem``).

    The optional fields mirror the TS conditional-include: ``complexity`` is
    ``None`` when the ticket has none; ``relevance_score`` is ``None`` when the
    accumulated score is ``0``; ``matched_files`` / ``match_reason`` are ``None``
    when empty.
    """

    id: str
    epic_id: str
    ticket_number: int | None
    title: str
    status: str
    tags: list[str]
    complexity: str | None = None
    relevance_score: int | None = None
    matched_files: list[str] | None = None
    match_reason: list[str] | None = None


@dataclass(frozen=True)
class SearchFilters:
    """Echo of the active filters (TS ``UnifiedSearchResult.filters``).

    A field is ``None`` when its filter was not supplied — the same
    conditional-include the TS does when assembling the response object.
    """

    query: str | None = None
    files: list[str] | None = None
    tags: list[str] | None = None
    match_all_tags: bool | None = None
    related_to: str | None = None
    status: list[str] | None = None
    complexity: list[str] | None = None


@dataclass(frozen=True)
class SearchResult:
    """The unified search response (TS ``UnifiedSearchResult``)."""

    tickets: list[SearchTicketItem]
    total: int
    has_more: bool
    filters: SearchFilters


# ---------------------------------------------------------------------------
# Glob → regex  (port of `matchGlob`, verbatim semantics)
# ---------------------------------------------------------------------------

# The regex specials the TS escapes before translating the glob wildcards:
# `. + ^ $ { } ( ) | [ ] \` — note `*` and `?` are deliberately NOT here (they are
# the glob wildcards handled next).
_REGEX_SPECIALS = re.compile(r"[.+^${}()|\[\]\\]")


def _glob_to_regex(pattern: str) -> str:
    """Translate a shell-glob into an anchored regex body (TS escape/replace chain).

    Order matters and matches the TS exactly: escape regex specials, protect ``**``
    (globstar) behind a sentinel, turn ``*`` into ``[^/]*`` (a single path segment)
    and ``?`` into ``[^/]`` (one non-slash char), then expand the globstar sentinel
    to ``.*`` (crosses path separators).
    """
    escaped = _REGEX_SPECIALS.sub(lambda m: "\\" + m.group(0), pattern)
    return (
        escaped.replace("**", "@@GLOBSTAR@@")
        .replace("*", "[^/]*")
        .replace("?", "[^/]")
        .replace("@@GLOBSTAR@@", ".*")
    )


def match_glob(pattern: str, text: str) -> bool:
    """Return whether *text* matches shell-glob *pattern* (port of ``matchGlob``).

    Three path-prefix short-circuits run first (exact equality; a trailing-``/``
    directory prefix; a ``pattern + '/'`` directory prefix), then the glob is
    converted to an anchored regex (``^…$`` ≡ :func:`re.fullmatch`). ``*`` stays
    within one path segment, ``?`` is a single non-slash char, and ``**`` crosses
    separators. A malformed regex fails closed (``False``), as the TS ``try/catch`` does.
    """
    if text == pattern:
        return True
    if pattern.endswith("/") and text.startswith(pattern):
        return True
    if text.startswith(pattern + "/"):
        return True
    try:
        return re.fullmatch(_glob_to_regex(pattern), text) is not None
    except re.error:
        return False


def _match_files_with_patterns(ticket_files: Sequence[str], patterns: Sequence[str]) -> list[str]:
    """Every ticket file matching at least one pattern (TS ``matchFilesWithPatterns``)."""
    matched: list[str] = []
    for file in ticket_files:
        for pattern in patterns:
            if match_glob(pattern, file):
                matched.append(file)
                break
    return matched


# ---------------------------------------------------------------------------
# Relevance scoring  (ports of `calculateRelevance` / `calculateRelatedScore`)
# ---------------------------------------------------------------------------


def _calculate_relevance(
    terms: Sequence[str],
    title_matches: Sequence[str],
    desc_matches: Sequence[str],
    title: str,
    description: str | None,
) -> int:
    """Additive query relevance (TS ``calculateRelevance``).

    Per-term hits: title ``×10``, description ``×3``. Then full-phrase bonuses: the
    joined query inside the title ``+20``, inside the description ``+10``, and every
    term appearing in title **or** description ``+15``.
    """
    score = len(title_matches) * 10 + len(desc_matches) * 3
    full_query = " ".join(terms)
    title_lower = title.lower()
    desc_lower = description.lower() if description is not None else None
    if full_query in title_lower:
        score += 20
    if desc_lower is not None and full_query in desc_lower:
        score += 10
    if all(t in title_lower or (desc_lower is not None and t in desc_lower) for t in terms):
        score += 15
    return score


def _calculate_related_score(ticket: SearchTicket, source: SearchTicket) -> tuple[int, list[str]]:
    """``relatedTo`` similarity (TS ``calculateRelatedScore``).

    Shared files ``×10`` (exact path match), shared tags ``×5`` (case-insensitive),
    same epic ``+15``. Returns the score and the human-readable match reasons.
    """
    score = 0
    reasons: list[str] = []

    shared_files = [f for f in ticket.files if f in source.files]
    if shared_files:
        score += len(shared_files) * 10
        reasons.append(f"{len(shared_files)} shared file(s)")

    normalized_source_tags = [t.lower() for t in source.tags]
    normalized_ticket_tags = [t.lower() for t in ticket.tags]
    shared_tags = [t for t in normalized_ticket_tags if t in normalized_source_tags]
    if shared_tags:
        score += len(shared_tags) * 5
        reasons.append(f"{len(shared_tags)} shared tag(s)")

    if ticket.epic_id == source.epic_id:
        score += 15
        reasons.append("same epic")

    return score, reasons


# ---------------------------------------------------------------------------
# Validation + query normalization
# ---------------------------------------------------------------------------


def _normalize_query(query: str | None) -> str | None:
    """Trim *query*; an empty/whitespace string collapses to ``None`` (TS ``trim`` + truthiness)."""
    if query is None:
        return None
    return query.strip() or None


def _validate_scope(
    project_id: str | None, specification_id: str | None, epic_id: str | None
) -> None:
    """Require one of project / specification / epic (TS scope guard)."""
    if project_id is None and specification_id is None and epic_id is None:
        raise ValidationFailedError("One of projectId, specificationId, or epicId is required.")


def _validate_filters(
    query: str | None,
    files: Sequence[str] | None,
    tags: Sequence[str] | None,
    related_to: str | None,
) -> None:
    """Require at least one of query / files / tags / relatedTo (TS filter guard)."""
    if not (query or files or tags or related_to):
        raise ValidationFailedError(
            "At least one filter is required: query, files, tags, or relatedTo"
        )


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def _score_ticket(
    ticket: SearchTicket,
    *,
    query: str | None,
    search_terms: Sequence[str],
    files: Sequence[str],
    normalized_tags: Sequence[str],
    match_all_tags: bool,
    related_to: str | None,
    related_source: SearchTicket | None,
    status: Sequence[str],
    complexity: Sequence[str],
) -> SearchTicketItem | None:
    """Apply every AND-combined filter to one ticket; ``None`` ⇒ filtered out.

    Each failed filter short-circuits (the TS ``continue``). Surviving tickets carry
    the accumulated relevance + match reasons.
    """
    if status and ticket.status not in status:
        return None
    if complexity and (ticket.complexity or "") not in complexity:
        return None

    relevance = 0
    reasons: list[str] = []
    matched_files: list[str] = []

    if query:
        title_lower = ticket.title.lower()
        desc_lower = (ticket.description or "").lower()
        title_matches = [t for t in search_terms if t in title_lower]
        desc_matches = [t for t in search_terms if t in desc_lower]
        if not title_matches and not desc_matches:
            return None
        relevance += _calculate_relevance(
            search_terms, title_matches, desc_matches, ticket.title, ticket.description
        )
        reasons.append("text match")

    if files:
        matched = _match_files_with_patterns(ticket.files, files)
        if not matched:
            return None
        matched_files = matched
        relevance += len(matched) * 10
        reasons.append(f"{len(matched)} file match(es)")

    if normalized_tags:
        normalized_ticket_tags = [t.lower().strip() for t in ticket.tags]
        matched_tags = [t for t in normalized_tags if t in normalized_ticket_tags]
        if match_all_tags:
            if len(matched_tags) != len(normalized_tags):
                return None
        elif not matched_tags:
            return None
        relevance += len(matched_tags) * 5
        reasons.append(f"{len(matched_tags)} tag match(es)")

    if related_to and related_source is not None:
        if ticket.id == related_to:
            return None
        related_score, related_reasons = _calculate_related_score(ticket, related_source)
        if related_score == 0:
            return None
        relevance += related_score
        reasons.extend(related_reasons)

    return SearchTicketItem(
        id=ticket.id,
        epic_id=ticket.epic_id,
        ticket_number=ticket.ticket_number,
        title=ticket.title,
        status=ticket.status,
        complexity=ticket.complexity,
        tags=list(ticket.tags),
        relevance_score=relevance if relevance > 0 else None,
        matched_files=matched_files or None,
        match_reason=reasons or None,
    )


def _execute(
    tickets: Sequence[SearchTicket],
    *,
    query: str | None,
    files: Sequence[str] | None,
    tags: Sequence[str] | None,
    match_all_tags: bool,
    related_to: str | None,
    related_source: SearchTicket | None,
    status: Sequence[str] | None,
    complexity: Sequence[str] | None,
    limit: int,
    offset: int,
) -> SearchResult:
    """Run the filter → score → sort → paginate pipeline over an already-scoped list.

    Assumes *query* is normalized and validation has run. Sorting by relevance is
    applied only when ``query`` or ``related_to`` is active (TS), and is made
    explicitly deterministic by carrying the scope index as the tiebreak (a stable
    sort over the already-deterministic scope order).
    """
    files_list = list(files or [])
    tags_list = list(tags or [])
    status_list = list(status or [])
    complexity_list = list(complexity or [])
    search_terms = query.lower().split() if query else []
    normalized_tags = [t.lower().strip() for t in tags_list]

    items: list[SearchTicketItem] = []
    for ticket in tickets:
        item = _score_ticket(
            ticket,
            query=query,
            search_terms=search_terms,
            files=files_list,
            normalized_tags=normalized_tags,
            match_all_tags=match_all_tags,
            related_to=related_to,
            related_source=related_source,
            status=status_list,
            complexity=complexity_list,
        )
        if item is not None:
            items.append(item)

    if query or related_to:
        items = [
            item
            for _, item in sorted(
                enumerate(items),
                key=lambda pair: (-(pair[1].relevance_score or 0), pair[0]),
            )
        ]

    total = len(items)
    paged = items[offset : offset + limit]
    return SearchResult(
        tickets=paged,
        total=total,
        has_more=offset + limit < total,
        filters=SearchFilters(
            query=query,
            files=files_list or None,
            tags=tags_list or None,
            match_all_tags=match_all_tags if tags_list else None,
            related_to=related_to,
            status=status_list or None,
            complexity=complexity_list or None,
        ),
    )


def run_search(
    tickets: Sequence[SearchTicket],
    *,
    query: str | None = None,
    files: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    match_all_tags: bool = False,
    related_to: str | None = None,
    related_source: SearchTicket | None = None,
    status: Sequence[str] | None = None,
    complexity: Sequence[str] | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Run the search pipeline over an in-memory, already-scoped *tickets* list.

    The pure half of :func:`search_tickets` (no DB, no scope resolution). Filter
    validation still applies. When ``related_to`` is given without an explicit
    ``related_source``, the source ticket is resolved from *tickets* by id (and a
    miss raises :class:`~specsmither.operations.errors.NotFoundError`).
    """
    normalized = _normalize_query(query)
    _validate_filters(normalized, files, tags, related_to)

    source = related_source
    if related_to and source is None:
        source = next((t for t in tickets if t.id == related_to), None)
        if source is None:
            raise NotFoundError(f"Ticket {related_to} not found.")

    return _execute(
        tickets,
        query=normalized,
        files=files,
        tags=tags,
        match_all_tags=match_all_tags,
        related_to=related_to,
        related_source=source,
        status=status,
        complexity=complexity,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# DB entry  (scope resolution over the SQLite stores)
# ---------------------------------------------------------------------------


def _record_to_search_ticket(record: TicketRecord) -> SearchTicket:
    """Flatten a hydrated :class:`TicketRecord` into a :class:`SearchTicket`.

    ``files`` is the union of the four ``files_to_be_*`` arrays (the TS file-change
    union); enums are unwrapped to their plain string ``value``.
    """
    files = (
        *record.files_to_be_created,
        *record.files_to_be_modified,
        *record.files_to_be_deleted,
        *record.files_to_be_referenced,
    )
    return SearchTicket(
        id=record.id,
        epic_id=record.epic_id,
        title=record.title,
        status=record.status.value,
        ticket_number=record.ticket_number,
        description=record.description,
        complexity=record.complexity.value if record.complexity is not None else None,
        tags=tuple(record.tags or ()),
        files=files,
    )


def _load_source_ticket(stores: AllStores, ticket_id: str) -> SearchTicket:
    """Load the ``relatedTo`` source ticket (TS ``getSourceTicketForRelated``).

    Loaded independently of the search scope — the source may live outside it. A
    missing id raises :class:`~specsmither.operations.errors.NotFoundError`.
    """
    record = stores.tickets.get_ticket(ticket_id)
    if record is None:
        raise NotFoundError(f"Ticket {ticket_id} not found.")
    return _record_to_search_ticket(record)


def _load_tickets_in_scope(
    stores: AllStores,
    *,
    project_id: str | None,
    specification_id: str | None,
    epic_id: str | None,
) -> list[SearchTicket]:
    """Resolve the epic / specification / project scope to a flat ticket list.

    Precedence epic → specification → project (TS ``getTicketsInScope``). The scope
    anchor must exist — ``get_epic`` / ``get_specification`` / ``get_project`` raise
    :class:`~specsmither.operations.errors.NotFoundError` when it does not, matching
    the TS ``*NotFoundError`` throws.
    """
    if epic_id is not None:
        stores.epics.get_epic(epic_id)  # existence check (raises NotFoundError)
        return [_record_to_search_ticket(t) for t in stores.tickets.list_tickets(epic_id=epic_id)]

    spec_ids: list[str] = []
    if specification_id is not None:
        stores.specifications.get_specification(specification_id)
        spec_ids = [specification_id]
    elif project_id is not None:
        stores.projects.get_project(project_id)
        spec_ids = [s.id for s in stores.specifications.list_specifications(project_id=project_id)]

    out: list[SearchTicket] = []
    for spec_id in spec_ids:
        for epic in stores.epics.list_epics(specification_id=spec_id):
            out.extend(
                _record_to_search_ticket(t) for t in stores.tickets.list_tickets(epic_id=epic.id)
            )
    return out


def search_tickets(
    session_factory: sessionmaker[Session],
    *,
    query: str | None = None,
    files: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    match_all_tags: bool = False,
    related_to: str | None = None,
    status: Sequence[str] | None = None,
    complexity: Sequence[str] | None = None,
    project_id: str | None = None,
    specification_id: str | None = None,
    epic_id: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> SearchResult:
    """Search tickets within a scope (port of ``searchTickets``; the DB entry).

    Validates that a scope and at least one filter are supplied, then — inside a
    short-lived **read** session (no recompute; reads never mutate derived state) —
    resolves the scope to a ticket list, loads the ``relatedTo`` source if any, and
    runs the pure :func:`run_search` pipeline.

    Raises :class:`~specsmither.operations.errors.ValidationFailedError` for a
    missing scope/filter and :class:`~specsmither.operations.errors.NotFoundError`
    for a missing scope anchor or ``relatedTo`` ticket.
    """
    normalized = _normalize_query(query)
    _validate_scope(project_id, specification_id, epic_id)
    _validate_filters(normalized, files, tags, related_to)

    with session_factory() as session:
        stores = make_stores(session)
        source = _load_source_ticket(stores, related_to) if related_to else None
        scoped = _load_tickets_in_scope(
            stores,
            project_id=project_id,
            specification_id=specification_id,
            epic_id=epic_id,
        )

    return _execute(
        scoped,
        query=normalized,
        files=files,
        tags=tags,
        match_all_tags=match_all_tags,
        related_to=related_to,
        related_source=source,
        status=status,
        complexity=complexity,
        limit=limit,
        offset=offset,
    )
