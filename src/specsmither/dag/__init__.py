"""DAG engine (pure, determinism-critical): status calculation, single-hop
cascade, dependency tree, critical path / blocker depth / cycle detection.

Pins determinism exactly where it changes outputs: half-up rounding via
``floor(x + 0.5)``; the critical-path ``lex_less`` tie-break; the ``0 → 1`` DP
weight; Tarjan SCC for cycles.
"""
