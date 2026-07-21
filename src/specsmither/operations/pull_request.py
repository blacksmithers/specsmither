"""``link_pull_request`` — the spec ⇄ PR association primitive.

Backed by the ``specification_prs`` table. The primitive links directly against the
SPECIFICATION (the dispatch facade resolves ticket → spec upstream when needed).

Idempotency on ``(specification_id, pr_number)`` is the contract — the
``UNIQUE(specification_id, pr_number)`` constraint backs it. A re-link of the same
pair returns the EXISTING row (``already_linked=True``) with no duplicate and no
error; the insert is isolated in a SAVEPOINT so a racing duplicate
(``IntegrityError``) degrades to the same idempotent read rather than poisoning the
transaction. PR linking does not change any derived count, so no recompute runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from specsmither.db.models import Specification, SpecificationPr
from specsmither.operations.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["LinkPullRequestResult", "link_pull_request"]


@dataclass(frozen=True)
class LinkPullRequestResult:
    """Outcome of :func:`link_pull_request` (``already_linked`` marks an idempotent hit)."""

    id: str
    specification_id: str
    pr_number: int
    url: str | None
    title: str | None
    state: str | None
    already_linked: bool


def _find_pr(session: Session, specification_id: str, pr_number: int) -> SpecificationPr | None:
    """Return the ``(specification_id, pr_number)`` PR row, or ``None``."""
    return session.execute(
        select(SpecificationPr).where(
            SpecificationPr.specification_id == specification_id,
            SpecificationPr.pr_number == pr_number,
        )
    ).scalar_one_or_none()


def _to_result(row: SpecificationPr, *, already_linked: bool) -> LinkPullRequestResult:
    return LinkPullRequestResult(
        id=row.id,
        specification_id=row.specification_id,
        pr_number=row.pr_number,
        url=row.url,
        title=row.title,
        state=row.state,
        already_linked=already_linked,
    )


def link_pull_request(
    session_factory: sessionmaker[Session],
    specification_id: str,
    pr_number: int,
    *,
    url: str | None = None,
    title: str | None = None,
    state: str | None = None,
) -> LinkPullRequestResult:
    """Link a pull request to a specification, idempotent on ``(spec, pr_number)``.

    Raises :class:`~specsmither.operations.errors.NotFoundError` if the spec is
    absent. A pair already linked returns the existing row unchanged
    (``already_linked=True``); a fresh pair inserts one row and returns it
    (``already_linked=False``).
    """
    with session_factory.begin() as session:
        if session.get(Specification, specification_id) is None:
            raise NotFoundError(f"Specification not found: {specification_id}")

        existing = _find_pr(session, specification_id, pr_number)
        if existing is not None:
            return _to_result(existing, already_linked=True)

        row = SpecificationPr(
            specification_id=specification_id,
            pr_number=pr_number,
            url=url,
            title=title,
            state=state,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            # A concurrent writer linked the same pair first — return it idempotently.
            raced = _find_pr(session, specification_id, pr_number)
            if raced is not None:
                return _to_result(raced, already_linked=True)
            raise
        return _to_result(row, already_linked=False)
