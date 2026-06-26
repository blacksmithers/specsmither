"""Scaffold smoke test: the package imports and its gate dependency resolves."""

from __future__ import annotations

import specsmither


def test_version() -> None:
    assert specsmither.__version__


def test_crucible_gate_available() -> None:
    # The planning gate is a hard dependency from day one.
    import crucible

    assert callable(crucible.validate)
    assert crucible.models.Specification is not None
