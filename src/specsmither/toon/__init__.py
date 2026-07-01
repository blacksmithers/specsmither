"""TOON (Token-Oriented Object Notation) — the MCP agent wire format.

A faithful, encode-only Python port of ``@toon-format/toon`` v2.1.0. TOON is a
compact, lossless encoding of the JSON data model that combines YAML-style
indentation with CSV-style tabular arrays, yielding roughly 44% fewer tokens
than JSON for the uniform-array-of-objects shapes the MCP layer emits.

Only :func:`encode` is public; parity with the upstream TypeScript ``encode`` is
pinned byte-for-byte by the committed golden fixtures (``tests/test_toon_golden.py``).
"""

from __future__ import annotations

from specsmither.toon.encoder import encode

__all__ = ["encode"]
