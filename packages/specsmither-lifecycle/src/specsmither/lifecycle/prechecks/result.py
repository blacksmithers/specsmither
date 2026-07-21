"""The shared pre-check result type — :class:`Accepted` | :class:`Denied`.

Every planning pre-check is a **pure** function that returns a
:data:`PrecheckResult`. This is a frozen-dataclass union with rich,
English-first semantics so the L4 (APS / CPS) pipeline can build a denial
envelope without re-deriving prose:

* :class:`Accepted` carries optional **accept-side signals** the pipeline reads:
  ``rollback`` (a *late* op rewinds the session to its native phase) and
  ``auto_transition`` (SPS on a ``draft`` spec must flip it to ``planning``).
  A bare :class:`Accepted` (both ``False``) is the common case.
* :class:`Denied` carries a stable machine ``code``, a fully-rendered English
  ``message`` (read verbatim by clients — never parsed), an optional structured
  ``context`` bag, and an optional ``blockers`` list (the per-finding gate prose
  surfaced by the CPS gate / the dependency-batch cycle paths).

The union is closed: a pre-check returns exactly one of the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Accepted",
    "Denied",
    "PrecheckResult",
]


@dataclass(frozen=True)
class Accepted:
    """A pre-check passed.

    Both fields default to ``False``, so a bare ``Accepted()`` is the ordinary
    "nothing more to signal" result. The two optional accept-side signals are
    each meaningful to exactly one verb in the L4 pipeline:

    * ``rollback`` — set by :func:`operation_allowed` when the call classifies as
      *late* (a structural body change run past its native phase): the APS
      pipeline rewinds the session to the op's native phase and records a
      rollback transition. A late ``update_*`` touching only ``fieldDeclarations``
      is exempt and keeps ``rollback=False`` (the N/A-declaration exemption).
    * ``auto_transition`` — set by :func:`spec_status_check` when SPS runs on a
      ``draft`` spec: the SPS verb flips the spec status to ``planning`` as part
      of minting the session.
    """

    rollback: bool = False
    auto_transition: bool = False


@dataclass(frozen=True)
class Denied:
    """A pre-check rejected the call.

    ``code`` is the stable machine identifier; ``message`` is the rendered English
    prose; ``context`` is the optional structured metadata bag (snake-cased and
    JSON-serialisable); ``blockers`` is the optional list of human-readable blocking
    reasons (the CPS gate's failing findings, or the dependency-batch cycle paths).
    """

    code: str
    message: str
    context: dict[str, Any] | None = None
    blockers: list[str] | None = None


#: The closed union every pre-check returns.
PrecheckResult = Accepted | Denied
