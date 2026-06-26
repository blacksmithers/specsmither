"""``reopen_specification`` — work item #18 (the lone finalize→planning transition).

A clean Python rewrite of ``packages/operations/src/operations/reopen.ts``,
rebound onto :class:`~specsmither.db.repositories.core_stores.SpecStoreSqlite` and
wrapped in the M0 mutation contract (its own ``Session.begin()`` + recompute).

Reopens a finalized specification back into planning. Precondition: the spec must
be in ``ready`` (the post-CPS terminal state for the planning phase); any other
status raises :class:`~specsmither.operations.errors.PreconditionFailedError` with
the actual status. On accept the spec transitions ``ready → planning`` and the
recompute worklist re-derives the owning project's spec buckets — so
``ready_spec_count`` decrements and ``planning_spec_count`` increments in the same
transaction (an explicit M0 acceptance case).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from specsmither.db.repositories import make_stores
from specsmither.domain.enums import SpecStatus
from specsmither.operations.errors import PreconditionFailedError
from specsmither.rollups.recompute import recompute

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.domain.records import SpecificationRecord

__all__ = ["reopen_specification"]

_REQUIRED_STATUS = SpecStatus.READY
_TARGET_STATUS = SpecStatus.PLANNING


def reopen_specification(
    session_factory: sessionmaker[Session], specification_id: str
) -> SpecificationRecord:
    """Transition a ``ready`` spec back to ``planning`` and recompute its project.

    Raises :class:`~specsmither.operations.errors.NotFoundError` if the spec is
    absent and :class:`~specsmither.operations.errors.PreconditionFailedError` if it
    is in any status other than ``ready``. Returns the re-read (now ``planning``)
    specification record.
    """
    with session_factory.begin() as session:
        stores = make_stores(session)
        spec = stores.specifications.get_specification(specification_id)  # raises NotFound
        if spec.status != _REQUIRED_STATUS:
            raise PreconditionFailedError(
                f"reopen_specification requires status '{_REQUIRED_STATUS.value}', "
                f"but the spec is '{spec.status.value}'.",
                context={
                    "specification_id": specification_id,
                    "expected_status": _REQUIRED_STATUS.value,
                    "actual_status": spec.status.value,
                },
            )

        updated = spec.model_validate(
            {**spec.model_dump(by_alias=False), "status": _TARGET_STATUS.value}
        )
        stores.specifications.update_specification(updated)
        recompute(session, spec_ids=[specification_id])
        return stores.specifications.get_specification(specification_id)
