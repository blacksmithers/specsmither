"""L7 acceptance for ``operations.search`` (work item #20).

Golden / deterministic coverage of the ported ``searchTickets`` behaviour:

1. **glob → regex** — ``match_glob`` segment vs globstar semantics, plus the
   path-prefix short-circuits (``api*`` matches ``apiClient``; ``?at`` matches
   ``cat`` / ``hat`` but not ``flat``).
2. **tag AND vs OR** — ``match_all_tags`` requires every tag; the default requires
   at least one.
3. **relevance ordering** — a title hit outranks a description-only hit.
4. **sort + pagination** — deterministic order under a scope-index tiebreak, and a
   correct ``total`` / ``has_more`` window.
5. **DB entry** — ``search_tickets`` over a real SQLite scope (project → spec →
   epic → tickets), the scope/filter validation guards, ``relatedTo`` similarity,
   and the NotFound seam.

The pure cases drive :func:`run_search` over an in-memory list of
:class:`SearchTicket`; the integration case drives :func:`search_tickets` over a
``tmp_path`` database seeded through the stores.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_session_factory
from specsmither.db.migrations import init_db
from specsmither.db.repositories import AllStores, ProjectRecord, make_stores
from specsmither.domain.enums import TicketStatus
from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord
from specsmither.operations.errors import NotFoundError, ValidationFailedError
from specsmither.operations.search import (
    SearchTicket,
    match_glob,
    run_search,
    search_tickets,
)

# --------------------------------------------------------------------------- #
# 1. glob -> regex                                                            #
# --------------------------------------------------------------------------- #


def test_match_glob_star_is_single_segment() -> None:
    assert match_glob("api*", "apiClient") is True
    assert match_glob("api*", "api") is True
    # `*` does not cross a path separator.
    assert match_glob("src/*", "src/main.py") is True
    assert match_glob("src/*", "src/sub/main.py") is False


def test_match_glob_question_is_single_char() -> None:
    assert match_glob("?at", "cat") is True
    assert match_glob("?at", "hat") is True
    # `?` matches exactly one char, so the 4-char "flat" must not match.
    assert match_glob("?at", "flat") is False


def test_match_glob_globstar_crosses_separators() -> None:
    assert match_glob("src/**", "src/a/b/c.py") is True
    assert match_glob("**/test_*.py", "pkg/sub/test_x.py") is True


def test_match_glob_prefix_short_circuits() -> None:
    # exact equality
    assert match_glob("README.md", "README.md") is True
    # trailing-slash directory prefix
    assert match_glob("src/", "src/main.py") is True
    # `pattern + '/'` directory prefix (pattern has no trailing slash)
    assert match_glob("src", "src/main.py") is True
    # a regex special in the pattern is escaped, not interpreted
    assert match_glob("a.b", "axb") is False
    assert match_glob("a.b", "a.b") is True


# --------------------------------------------------------------------------- #
# 2. tag AND vs OR                                                            #
# --------------------------------------------------------------------------- #


def _tagged(ticket_id: str, tags: tuple[str, ...]) -> SearchTicket:
    return SearchTicket(
        id=ticket_id,
        epic_id="epic-1",
        title=f"Ticket {ticket_id}",
        status="pending",
        ticket_number=int(ticket_id.split("-")[-1]),
        tags=tags,
    )


def test_tag_or_matches_any() -> None:
    tickets = [
        _tagged("t-1", ("api", "backend")),
        _tagged("t-2", ("frontend",)),
        _tagged("t-3", ("api",)),
    ]
    result = run_search(tickets, tags=["api", "frontend"], match_all_tags=False)
    assert {t.id for t in result.tickets} == {"t-1", "t-2", "t-3"}


def test_tag_and_requires_all() -> None:
    tickets = [
        _tagged("t-1", ("api", "backend")),
        _tagged("t-2", ("api",)),
        _tagged("t-3", ("api", "backend", "extra")),
    ]
    result = run_search(tickets, tags=["api", "backend"], match_all_tags=True)
    # t-2 lacks "backend" → excluded; t-1 and t-3 carry both.
    assert {t.id for t in result.tickets} == {"t-1", "t-3"}


def test_tag_matching_is_case_and_whitespace_insensitive() -> None:
    tickets = [_tagged("t-1", ("API", " Backend "))]
    result = run_search(tickets, tags=["api", "backend"], match_all_tags=True)
    assert [t.id for t in result.tickets] == ["t-1"]


# --------------------------------------------------------------------------- #
# 3. relevance ordering                                                       #
# --------------------------------------------------------------------------- #


def test_title_hit_outranks_description_only_hit() -> None:
    tickets = [
        SearchTicket(
            id="desc-only",
            epic_id="epic-1",
            title="Unrelated heading",
            status="pending",
            ticket_number=1,
            description="this body mentions authentication once",
        ),
        SearchTicket(
            id="title-hit",
            epic_id="epic-1",
            title="Authentication flow",
            status="pending",
            ticket_number=2,
            description="no body keyword here",
        ),
    ]
    result = run_search(tickets, query="authentication")
    assert [t.id for t in result.tickets] == ["title-hit", "desc-only"]
    title_item = result.tickets[0]
    desc_item = result.tickets[1]
    assert title_item.relevance_score is not None
    assert desc_item.relevance_score is not None
    assert title_item.relevance_score > desc_item.relevance_score
    assert title_item.match_reason == ["text match"]


def test_query_substring_matches_midword() -> None:
    # Substring, not tokenised: "thenti" inside "authentication" must hit.
    tickets = [
        SearchTicket(
            id="t-1",
            epic_id="epic-1",
            title="Authentication",
            status="pending",
            ticket_number=1,
        )
    ]
    result = run_search(tickets, query="thenti")
    assert [t.id for t in result.tickets] == ["t-1"]


# --------------------------------------------------------------------------- #
# 4. sort + pagination determinism                                           #
# --------------------------------------------------------------------------- #


def test_sort_is_deterministic_for_equal_scores() -> None:
    # Three tickets all matching the query with identical scores; the scope order
    # (the input order) is preserved as the tiebreak.
    tickets = [
        SearchTicket(id="t-1", epic_id="e", title="alpha match", status="pending", ticket_number=1),
        SearchTicket(id="t-2", epic_id="e", title="alpha match", status="pending", ticket_number=2),
        SearchTicket(id="t-3", epic_id="e", title="alpha match", status="pending", ticket_number=3),
    ]
    result = run_search(tickets, query="alpha")
    assert [t.id for t in result.tickets] == ["t-1", "t-2", "t-3"]
    scores = {t.relevance_score for t in result.tickets}
    assert len(scores) == 1  # all equal


def test_pagination_window_and_has_more() -> None:
    tickets = [
        SearchTicket(
            id=f"t-{i}",
            epic_id="e",
            title="match",
            status="pending",
            ticket_number=i,
        )
        for i in range(5)
    ]
    page1 = run_search(tickets, query="match", limit=2, offset=0)
    assert page1.total == 5
    assert page1.has_more is True
    assert [t.id for t in page1.tickets] == ["t-0", "t-1"]

    page3 = run_search(tickets, query="match", limit=2, offset=4)
    assert page3.total == 5
    assert page3.has_more is False
    assert [t.id for t in page3.tickets] == ["t-4"]


def test_no_sort_without_query_or_related() -> None:
    # tags-only search keeps scope order (no relevance sort applied).
    tickets = [_tagged("t-3", ("x",)), _tagged("t-1", ("x",)), _tagged("t-2", ("x",))]
    result = run_search(tickets, tags=["x"])
    assert [t.id for t in result.tickets] == ["t-3", "t-1", "t-2"]


# --------------------------------------------------------------------------- #
# 5. status / complexity / related filters (pure)                            #
# --------------------------------------------------------------------------- #


def test_status_and_complexity_filters() -> None:
    tickets = [
        SearchTicket(
            id="t-1", epic_id="e", title="api work", status="ready",
            ticket_number=1, complexity="high",
        ),
        SearchTicket(
            id="t-2", epic_id="e", title="api work", status="done",
            ticket_number=2, complexity="low",
        ),
    ]
    by_status = run_search(tickets, query="api", status=["ready"])
    assert [t.id for t in by_status.tickets] == ["t-1"]
    by_complexity = run_search(tickets, query="api", complexity=["low"])
    assert [t.id for t in by_complexity.tickets] == ["t-2"]


def test_related_to_scores_shared_files_tags_and_epic() -> None:
    source = SearchTicket(
        id="src",
        epic_id="epic-1",
        title="source",
        status="pending",
        ticket_number=1,
        tags=("api",),
        files=("src/a.py",),
    )
    tickets = [
        source,
        SearchTicket(
            id="same-epic-shared",
            epic_id="epic-1",
            title="neighbour",
            status="pending",
            ticket_number=2,
            tags=("api",),
            files=("src/a.py",),
        ),
        SearchTicket(
            id="unrelated",
            epic_id="epic-2",
            title="far",
            status="pending",
            ticket_number=3,
            tags=("other",),
            files=("src/z.py",),
        ),
    ]
    result = run_search(tickets, related_to="src")
    # source is excluded (self); unrelated scores 0 and drops out.
    assert [t.id for t in result.tickets] == ["same-epic-shared"]
    hit = result.tickets[0]
    # shared file (10) + shared tag (5) + same epic (15) = 30
    assert hit.relevance_score == 30
    assert hit.match_reason == ["1 shared file(s)", "1 shared tag(s)", "same epic"]


def test_run_search_related_to_missing_source_raises() -> None:
    tickets = [_tagged("t-1", ("x",))]
    with pytest.raises(NotFoundError):
        run_search(tickets, related_to="nope")


def test_run_search_requires_a_filter() -> None:
    tickets = [_tagged("t-1", ("x",))]
    with pytest.raises(ValidationFailedError):
        run_search(tickets)


# --------------------------------------------------------------------------- #
# 6. DB entry: search_tickets over a real SQLite scope                        #
# --------------------------------------------------------------------------- #

PROJECT_ID = "proj-1"
SPEC_ID = "spec-1"
EPIC_ID = "epic-1"


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = init_db(tmp_path / "search.db")
    return make_session_factory(engine)


def _seed(stores: AllStores) -> None:
    stores.projects.create_project(ProjectRecord(id=PROJECT_ID, name="P"))
    stores.specifications.create_specification(
        SpecificationRecord(id=SPEC_ID, project_id=PROJECT_ID, title="S")
    )
    stores.epics.create_epic(
        EpicRecord(
            id=EPIC_ID,
            specification_id=SPEC_ID,
            title="E",
            description="d",
            objective="o",
            epic_number=1,
            order=0,
        )
    )
    stores.tickets.create_ticket(
        TicketRecord(
            id="ticket-auth",
            epic_id=EPIC_ID,
            title="Authentication flow",
            ticket_number=1,
            order=0,
            status=TicketStatus.READY,
            tags=["api", "security"],
            files_to_be_modified=["src/auth.py"],
        )
    )
    stores.tickets.create_ticket(
        TicketRecord(
            id="ticket-ui",
            epic_id=EPIC_ID,
            title="Dashboard layout",
            ticket_number=2,
            order=1,
            status=TicketStatus.PENDING,
            description="renders the authentication banner",
            tags=["frontend"],
            files_to_be_created=["src/ui/dash.py"],
        )
    )


def test_search_tickets_db_query_and_scope(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        _seed(make_stores(session))

    result = search_tickets(factory, query="authentication", specification_id=SPEC_ID)
    # Both tickets mention "authentication" (title vs description); title hit ranks first.
    assert [t.id for t in result.tickets] == ["ticket-auth", "ticket-ui"]
    assert result.total == 2
    assert result.filters.query == "authentication"

    # Project scope resolves the same tickets through spec → epic fan-out.
    by_project = search_tickets(factory, query="authentication", project_id=PROJECT_ID)
    assert {t.id for t in by_project.tickets} == {"ticket-auth", "ticket-ui"}


def test_search_tickets_db_tag_and_file_filters(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        _seed(make_stores(session))

    by_tag = search_tickets(factory, tags=["api"], epic_id=EPIC_ID)
    assert [t.id for t in by_tag.tickets] == ["ticket-auth"]

    by_glob = search_tickets(factory, files=["src/ui/*"], epic_id=EPIC_ID)
    assert [t.id for t in by_glob.tickets] == ["ticket-ui"]
    assert by_glob.tickets[0].matched_files == ["src/ui/dash.py"]


def test_search_tickets_db_related_to(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        _seed(make_stores(session))

    result = search_tickets(factory, related_to="ticket-auth", epic_id=EPIC_ID)
    # ticket-ui shares the epic with the source → scores (same epic), ticket-auth is self.
    assert [t.id for t in result.tickets] == ["ticket-ui"]
    assert result.tickets[0].relevance_score == 15


def test_search_tickets_validation_and_not_found(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    with factory.begin() as session:
        _seed(make_stores(session))

    # No scope supplied.
    with pytest.raises(ValidationFailedError):
        search_tickets(factory, query="x")
    # Scope present but no filter.
    with pytest.raises(ValidationFailedError):
        search_tickets(factory, epic_id=EPIC_ID)
    # Missing scope anchor.
    with pytest.raises(NotFoundError):
        search_tickets(factory, query="x", epic_id="nope")
    # Missing relatedTo ticket.
    with pytest.raises(NotFoundError):
        search_tickets(factory, related_to="nope", epic_id=EPIC_ID)
