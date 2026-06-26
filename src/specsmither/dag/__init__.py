"""DAG engine (pure, determinism-critical): status calculation, single-hop
cascade, dependency tree, critical path / blocker depth / cycle detection.

Replicates TS determinism exactly where it changes outputs: ``Math.round``
half-up via ``floor(x + 0.5)``; critical-path ``lexLess`` tie-break; the
``0 → 1`` DP weight; Tarjan SCC for cycles.
"""
