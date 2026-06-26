"""L1 semantic CRUD errors + the SQLite/SQLAlchemy → CrudError bridge.

Clean Python rewrite of ``operations/errors/*`` (the TS ``CrudError`` hierarchy
+ the L0→L1 ``toCrudError`` bridge). The four CRUD codes — ``NOT_FOUND``,
``VALIDATION_FAILED``, ``CONFLICT``, ``PRECONDITION_FAILED`` — are the throw site
the verbs use; :func:`to_crud_error` translates a low-level persistence failure
(a SQLAlchemy / sqlite3 constraint violation) into the matching semantic error.

Faithful-port decisions:

* The TS JS-brand hack (``CRUD_ERROR_BRAND`` / ``__sfErrorBrand``, a guard
  against Lambda-bundle class duplication) is **dropped**. Python has a single
  class identity per process, so real exception classes + ``isinstance`` are the
  guard — :func:`isinstance(err, CrudError) <isinstance>` replaces ``isCrudError``.
* The per-entity ``NotFoundError`` subclasses (``SpecNotFoundError`` … ) and the
  ``ErrorGuidanceEntityType`` label table are a dispatch/UX nicety, not part of
  this work item's M0 contract; callers pass an English ``message`` directly.
* The ``conditional-check-failed`` disambiguation (CONFLICT on a create/start
  verb vs PRECONDITION_FAILED on a transition/reopen verb) is preserved and
  driven off the ``intent`` argument of :func:`to_crud_error`.

Guidance seam (M0 = LIGHT): each error carries an optional ``next_actions``
(a list of English next-step sentences). The full ``compose*ErrorGuidance``
pipeline + the 7-code ``ErrorGuidanceCode`` dispatch surface
(``UNAUTHORISED`` / ``UNKNOWN_TOOL`` / ``INTERNAL`` on top of these four, plus
the ``ErrorGuidance{prose, next_actions, related}`` composer) is a 0.1.0
dispatch-facade concern and is intentionally **not** built here. This module
depends only on stdlib + sqlalchemy (no domain/enums coupling).
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy.exc import IntegrityError

__all__ = [
    "ConflictError",
    "CrudError",
    "CrudErrorCode",
    "NotFoundError",
    "PreconditionFailedError",
    "SpecSmitherError",
    "ValidationFailedError",
    "to_crud_error",
]


class SpecSmitherError(Exception):
    """Root of the SpecSmither exception tree.

    Everything the engine raises deliberately derives from this, so a caller can
    distinguish an engine error from an arbitrary stdlib/third-party exception
    with a single ``isinstance`` check.
    """


class CrudErrorCode(StrEnum):
    """The four MCP CRUD error codes (verbatim values; they round-trip on wire).

    The wider 7-code ``ErrorGuidanceCode`` surface
    (``UNAUTHORISED`` / ``UNKNOWN_TOOL`` / ``INTERNAL``) is a dispatch concern and
    lives outside this module — see the module docstring seam note.
    """

    NOT_FOUND = "NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CONFLICT = "CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"


class CrudError(SpecSmitherError):
    """Base for the four semantic CRUD errors thrown by the operations verbs.

    Carries the MCP ``code``, an English ``message`` (read verbatim by clients —
    never parsed), an optional structured ``context`` bag, and an optional
    ``next_actions`` list (the M0 LIGHT guidance seam: plain English next-step
    sentences). Concrete subclasses fix ``code``; construct those, not this base.
    """

    def __init__(
        self,
        code: CrudErrorCode,
        message: str,
        *,
        context: dict[str, object] | None = None,
        next_actions: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context
        self.next_actions = next_actions
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        return self.message


class NotFoundError(CrudError):
    """An id resolved nothing."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, object] | None = None,
        next_actions: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            CrudErrorCode.NOT_FOUND,
            message,
            context=context,
            next_actions=next_actions,
            cause=cause,
        )


class ValidationFailedError(CrudError):
    """A shape/constraint check rejected an argument (FK / NOT NULL / CHECK)."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, object] | None = None,
        next_actions: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            CrudErrorCode.VALIDATION_FAILED,
            message,
            context=context,
            next_actions=next_actions,
            cause=cause,
        )


class ConflictError(CrudError):
    """A create/start verb collided with a pre-existing entity (UNIQUE clash)."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, object] | None = None,
        next_actions: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            CrudErrorCode.CONFLICT,
            message,
            context=context,
            next_actions=next_actions,
            cause=cause,
        )


class PreconditionFailedError(CrudError):
    """A transition/lock verb hit a state it cannot act on (UNIQUE-guarded lock)."""

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, object] | None = None,
        next_actions: list[str] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            CrudErrorCode.PRECONDITION_FAILED,
            message,
            context=context,
            next_actions=next_actions,
            cause=cause,
        )


# Disambiguates a UNIQUE-constraint violation by verb intent. A racing
# create/add collides (CONFLICT, the default); a transition/lock verb that loses
# the one-active-session (or per-ticket) partial-unique race hit a bad
# precondition (PRECONDITION_FAILED).
_TRANSITION_INTENTS = frozenset({"transition", "lock"})


def _orig_message(exc: IntegrityError) -> str:
    """Best-effort text to classify on — the wrapped DBAPI (sqlite3) message.

    SQLAlchemy stashes the underlying ``sqlite3`` exception on ``.orig``; its
    ``str()`` is the canonical ``"<KIND> constraint failed: table.column"`` text.
    Falls back to the wrapper's own ``str()`` when ``.orig`` is absent.
    """
    orig = exc.orig
    return str(orig) if orig is not None else str(exc)


def to_crud_error(exc: Exception, *, intent: str = "mutate") -> CrudError:
    """Map a persistence failure to its semantic :class:`CrudError`.

    * An existing :class:`CrudError` passes through unchanged.
    * A SQLAlchemy :class:`~sqlalchemy.exc.IntegrityError` is classified off its
      wrapped ``.orig`` message:

      - ``UNIQUE constraint failed`` → :class:`ConflictError` for a create/add
        intent (the default ``mutate`` also collides), or
        :class:`PreconditionFailedError` for a ``transition``/``lock`` intent.
      - ``FOREIGN KEY`` / ``NOT NULL`` / ``CHECK`` (and any other integrity
        violation) → :class:`ValidationFailedError`.

    * Any other exception → :class:`ValidationFailedError` (the conservative M0
      bucket; a richer ``INTERNAL`` mapping is a 0.1.0 dispatch concern).

    The original exception is always preserved as ``__cause__``.
    """
    if isinstance(exc, CrudError):
        return exc

    if isinstance(exc, IntegrityError):
        detail = _orig_message(exc)
        lowered = detail.lower()
        context: dict[str, object] = {"intent": intent, "detail": detail}

        if "unique constraint failed" in lowered:
            if intent in _TRANSITION_INTENTS:
                return PreconditionFailedError(
                    f"Precondition failed: {detail}", context=context, cause=exc
                )
            # create / add — and the default mutate intent — are collisions.
            return ConflictError(
                f"Conflict: {detail}", context=context, cause=exc
            )

        # FOREIGN KEY / NOT NULL / CHECK — and any other integrity violation —
        # are data problems the caller can fix.
        return ValidationFailedError(
            f"Validation failed: {detail}", context=context, cause=exc
        )

    return ValidationFailedError(
        f"Mutation failed: {exc}",
        context={"intent": intent, "origin": type(exc).__name__},
        cause=exc,
    )
