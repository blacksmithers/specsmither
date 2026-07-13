"""Id + timestamp generators (ULID string ids; ISO-8601 UTC string timestamps).

A dependency-free leaf: importing this pulls in ``ulid`` / ``datetime`` /
``threading`` and NOTHING else. It lives outside :mod:`specsmither.db.base` so the
pure lifecycle path can mint ids and timestamps without dragging sqlalchemy in.

* :func:`new_ulid` — a fresh 26-char ULID string, STRICTLY monotonic.
* :func:`now_iso` — the current instant as an ISO-8601 UTC string.

:mod:`specsmither.db.base` re-imports both (its ``IdMixin`` / ``TimestampMixin``
column defaults need them) and re-exports them for back-compat.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from ulid import ULID

__all__ = [
    "new_ulid",
    "now_iso",
]


_ULID_LOCK = threading.Lock()
_LAST_ULID: ULID | None = None


def new_ulid() -> str:
    """Return a fresh 26-char ULID string, STRICTLY monotonic (mint order == sort order).

    ``ulid.ULID()`` is only monotonic across milliseconds — two ids minted in the
    same millisecond fall back to random ordering. The append-only planning audit
    rows (actions / transitions / datapoints) are read back ordered by id and
    interleaved by the TUI; without a strict guarantee, two rows written in one
    transaction (e.g. a phase-advance + the human-approve action) could sort in
    either order, making the action log — and its snapshot — flaky. Bumping any
    collision by one int makes lexicographic id order equal insertion order.
    """
    global _LAST_ULID
    with _ULID_LOCK:
        candidate = ULID()
        if _LAST_ULID is not None and candidate <= _LAST_ULID:
            candidate = ULID.from_int(int(_LAST_ULID) + 1)
        _LAST_ULID = candidate
        return str(candidate)


def now_iso() -> str:
    """Return the current instant as an ISO-8601 UTC string (e.g. ``...+00:00``)."""
    return datetime.now(tz=UTC).isoformat()
