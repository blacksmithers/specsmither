"""The coupling gate for the ``specsmither-lifecycle`` extraction.

Encodes the acceptance criterion "``import specsmither.lifecycle`` traz um
ambiente SEM sqlalchemy/textual/mcp". Each check runs in a FRESH subprocess (the
in-process ``sys.modules`` is polluted by the rest of the suite) that imports only
the pure verb surface and reports which heavy packages got pulled in.

Current status:
* ``textual`` / ``mcp`` — already fully decoupled -> these assertions PASS today.
* ``sqlalchemy`` — the three sinks are cut (the ``new_ulid``/``now_iso`` leaf, the
  ``WritePlan`` dataclasses lifted out of the executor module, and the ORM
  ``PlanningSession`` replaced by the pure ``PlanningSessionRecord`` on the verb
  surface). The pure import path is now sqlalchemy-free, so this assertion is a
  plain hard pass — it fails loudly if any edge regresses onto the path.
"""

from __future__ import annotations

import subprocess
import sys

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


def test_pure_surface_does_not_import_sqlalchemy() -> None:
    assert not _loaded_after_pure_import("sqlalchemy")
