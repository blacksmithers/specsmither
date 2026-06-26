"""Ticket child-backed-array tables (decompose-on-write / recompose-on-read).

Five (+ snippets) normalized child tables that back the array-valued fields of a
``Ticket`` (owned by :mod:`specsmither.db.models.core`). Each row hangs off its
parent ticket by a ``ticket_id`` foreign key with ``ON DELETE CASCADE`` — deleting
a ticket drops all of its acceptance criteria, implementation steps, planned file
changes, planned tests, and code/type snippets.

The repository layer (L5) treats every one of these as **replace-all**: on write
it deletes the ticket's existing rows and re-inserts the new set; on read it
recomposes the ordered arrays back into the crucible domain shapes
(``AcceptanceCriterion``, ``ImplementationStep``, ``CodeSnippet``, ``TypeSnippet``)
plus the ``TicketFileChange`` / planned-test views. Because the rows are
regenerated wholesale on every write they carry no audit timestamps — only the
ULID primary key (``IdMixin``) and their data columns.

Faithful-port corrections applied here:

* ``acceptance_criteria`` is the BDD triple ``given`` / ``when`` / ``then``
  (matching ``crucible.models.AcceptanceCriterion`` and the live amplify model),
  NOT a flat ``description`` — the architecture §3 "description" text is stale.
* ``ticket_file_changes`` / ``ticket_tests`` drop the DynamoDB-GSI ``projectId``
  denormalization; in SQLite the project is a cheap join (recon A5 §5).

``given`` / ``when`` / ``then`` / ``text`` / ``order`` are valid Python identifiers
and valid SQLite column names (SQLAlchemy auto-quotes ``order`` and the SQL
keywords), so they are used directly as attribute and column names.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from specsmither.db.base import Base, IdMixin

__all__ = [
    "AcceptanceCriterion",
    "CodeSnippet",
    "ImplementationStep",
    "TicketFileChange",
    "TicketTest",
    "TypeSnippet",
]


class AcceptanceCriterion(Base, IdMixin):
    """A BDD acceptance-criterion row for a ticket (``given`` / ``when`` / ``then``)."""

    __tablename__ = "acceptance_criteria"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    given: Mapped[str] = mapped_column()
    when: Mapped[str] = mapped_column()
    then: Mapped[str] = mapped_column()
    order: Mapped[int] = mapped_column()


class ImplementationStep(Base, IdMixin):
    """One ordered implementation-step row for a ticket."""

    __tablename__ = "implementation_steps"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column()
    order: Mapped[int] = mapped_column()


class TicketFileChange(Base, IdMixin):
    """A planned file change for a ticket. ``kind`` is a ``FileChangeKind`` value
    (``toBeCreated`` / ``toBeModified`` / ``toBeDeleted`` / ``toBeReferenced``)."""

    __tablename__ = "ticket_file_changes"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column()
    kind: Mapped[str] = mapped_column()
    order: Mapped[int | None] = mapped_column()


class TicketTest(Base, IdMixin):
    """A planned-test row for a ticket. Only the ``test_type`` axis (a crucible
    ``TestType`` value: ``unit`` / ``integration`` / ``e2e``) is normalized here;
    ``quality_gates`` / ``test_commands`` / ``coverage_target`` stay flat on the
    ticket row (owned by :mod:`specsmither.db.models.core`)."""

    __tablename__ = "ticket_tests"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    test_type: Mapped[str] = mapped_column()
    order: Mapped[int] = mapped_column()


class CodeSnippet(Base, IdMixin):
    """A code snippet attached to a ticket. Recomposes to
    ``crucible.models.CodeSnippet`` (needs at least ``id`` + ``content``);
    ``language`` / ``description`` are kept for fidelity, ``file_path`` for context."""

    __tablename__ = "code_snippets"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str | None] = mapped_column()
    file_path: Mapped[str | None] = mapped_column()
    description: Mapped[str | None] = mapped_column()
    content: Mapped[str] = mapped_column()
    order: Mapped[int | None] = mapped_column()


class TypeSnippet(Base, IdMixin):
    """A type snippet attached to a ticket. Recomposes to
    ``crucible.models.TypeSnippet`` (needs at least ``id`` + ``content``);
    ``language`` / ``description`` are kept for fidelity, ``file_path`` for context."""

    __tablename__ = "type_snippets"

    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str | None] = mapped_column()
    file_path: Mapped[str | None] = mapped_column()
    description: Mapped[str | None] = mapped_column()
    content: Mapped[str] = mapped_column()
    order: Mapped[int | None] = mapped_column()
