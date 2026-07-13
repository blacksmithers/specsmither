"""The coupling gate for the ``specsmither-lifecycle`` extraction.

Encodes the acceptance criterion "``import specsmither.lifecycle`` traz um
ambiente SEM sqlalchemy/textual/mcp". Each check runs in a FRESH subprocess (the
in-process ``sys.modules`` is polluted by the rest of the suite) that imports only
the pure verb surface and reports which heavy packages got pulled in.

Current status:
* ``textual`` / ``mcp`` — already fully decoupled -> these assertions PASS today.
* ``sqlalchemy`` — still pulled via three sinks (the ``new_ulid``/``now_iso`` leaf,
  the ``WritePlan`` dataclasses living in the executor module, and the ORM
  ``PlanningSession`` constructed by the verbs). Marked ``xfail(strict=True)`` so it
  FLIPS to a hard pass the moment the extraction cuts those edges — and fails loudly
  if someone claims it is done while sqlalchemy is still on the path.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: The pure surface a host embedder would import.
_PURE_IMPORTS = (
    "import specsmither.lifecycle.verbs.action, "
    "specsmither.lifecycle.verbs.start, "
    "specsmither.lifecycle.verbs.complete, "
    "specsmither.lifecycle.verbs.approve, "
    "specsmither.lifecycle.verbs.reject, "
    "specsmither.lifecycle.verbs.reject_with_feedback"
)


def _loaded_after_pure_import(package: str) -> bool:
    """True iff importing the pure verb surface pulls ``package`` into sys.modules.

    Runs in a clean subprocess so the result reflects the pure path alone, not the
    ambient modules the test session already imported.
    """
    probe = (
        f"{_PURE_IMPORTS}\n"
        "import sys\n"
        f"print(any(m == {package!r} or m.startswith({package + '.'!r}) for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip() == "True"


def test_pure_surface_does_not_import_textual() -> None:
    assert not _loaded_after_pure_import("textual")


def test_pure_surface_does_not_import_mcp() -> None:
    assert not _loaded_after_pure_import("mcp")


@pytest.mark.xfail(
    strict=True,
    reason="sinks A/B/C not yet cut: new_ulid/now_iso in db.base, WritePlan dataclasses in "
    "the executor module, and the ORM PlanningSession constructed by the verbs still pull "
    "sqlalchemy onto the pure path. Flips to pass when the extraction lands.",
)
def test_pure_surface_does_not_import_sqlalchemy() -> None:
    assert not _loaded_after_pure_import("sqlalchemy")
