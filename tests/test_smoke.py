"""Scaffold smoke test: the package imports and its gate dependency resolves."""

from __future__ import annotations

import importlib.metadata

import specsmither  # noqa: F401  (the PEP 420 namespace root must import)


def test_version() -> None:
    # ``specsmither`` is a PEP 420 namespace shared by the ``specsmither`` (product)
    # and ``specsmither-lifecycle`` (pure core) distributions, so the version lives in
    # the distribution metadata rather than on a namespace ``__init__``.
    assert importlib.metadata.version("specsmither")


def test_crucible_gate_available() -> None:
    # The planning gate is a hard dependency from day one.
    import crucible

    assert callable(crucible.validate)
    assert crucible.models.Specification is not None
