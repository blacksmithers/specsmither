"""Critical path / blocker depth / cycle detection — the determinism heart.

Wires the tree aggregators (critical path, blocker depth, cycle detection) into
the ``build-dependency-tree`` mapping that turns the raw ``{path, minutes}`` DP
result into :class:`CriticalPathNode`s. Outputs are pinned by golden fixtures, so
the determinism traps are enforced exactly:

* **Half-up rounding** (rule 1): half rounds toward +inf;
  :func:`round_half_up` uses ``floor(x + 0.5)`` (NOT Python ``round``/banker's).
* **0 -> 1 DP weight** (rule 2): a missing/zero estimate weighs **1** in path
  *selection* (``estimated_minutes > 0 ? estimated_minutes : 1``) but the
  displayed :attr:`CriticalPathNode.estimated_minutes` defaults a *missing*
  estimate to **60** (a literal ``0`` renders as ``0``, not 60).
* **lex_less tie-break** (rule 3): equal path-minutes -> the lexicographically
  smaller id-list wins; :func:`lex_less` is an element-wise compare where a
  shorter list wins on a shared prefix.
* **Sorted adjacency / roots** (rule 4): dependency lists are deduped+sorted and
  nodes/roots are iterated in sorted id order, giving a stable ASCII ordering.
* **Iterative Tarjan SCC** (rule 5): :func:`find_cycles` uses an explicit work
  stack (no recursion), emits SCCs of size >= 2 *plus* self-loops, sorts each
  cycle and the result by ``cycle[0]``.
* **Incomplete-blocker chain** (rule 6): :func:`compute_max_blocker_depth`
  edge-counts the longest chain of incomplete blockers; a ``done`` dependency is
  dropped and terminates the chain; a ``visiting`` guard makes a residual cycle's
  back-edge contribute 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from specsmither.dag.types import DepEdge, TicketNode

__all__ = [
    "UNNUMBERED_PLACEHOLDER",
    "CriticalPathNode",
    "CriticalPathResult",
    "compute_critical_path",
    "compute_max_blocker_depth",
    "find_cycles",
    "lex_less",
    "round_half_up",
]

#: Placeholder for a missing ``ticket_number``/``title`` (build-tree guards).
#: A missing number renders as ``0``; a missing title as this.
UNNUMBERED_PLACEHOLDER = "<unnumbered>"

#: Shared immutable default so ``frozenset()`` is never a function-call default.
_NO_EXCLUDE: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CriticalPathNode:
    """A node on the critical path.

    :attr:`estimated_minutes` carries the **display** default: a *missing*
    estimate renders 60, a literal 0 renders 0.
    """

    id: str
    ticket_number: int
    title: str
    estimated_minutes: int


@dataclass(frozen=True, slots=True)
class CriticalPathResult:
    """Everything the dependency-tree builder consumes from the critical path.

    :attr:`path` is the list of :class:`CriticalPathNode`, :attr:`length` the node
    count, and :attr:`minutes` the half-up round of the summed DP weights (NOT the
    summed display values).
    """

    path: tuple[CriticalPathNode, ...]
    length: int
    minutes: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Internal DP result: ``{path, minutes}``."""

    path: tuple[str, ...]
    minutes: int


def round_half_up(value: float) -> int:
    """Round half toward +inf.

    ``round_half_up(0.5) == 1`` and ``round_half_up(2.5) == 3`` where Python's
    banker's ``round`` would yield ``0`` and ``2``. Use this for every rounded
    value (``critical_path_minutes`` and, downstream, ``progress``).
    """

    return math.floor(value + 0.5)


def lex_less(a: Sequence[str], b: Sequence[str]) -> bool:
    """Element-wise lexicographic ``a < b`` over id-lists.

    Compares element by element; the first differing element decides. On a shared
    prefix the **shorter** list is "less".
    """

    n = min(len(a), len(b))
    for i in range(n):
        if a[i] < b[i]:
            return True
        if a[i] > b[i]:
            return False
    return len(a) < len(b)


def compute_critical_path(
    tickets: Iterable[TicketNode],
    edges: Iterable[DepEdge],
    *,
    exclude: frozenset[str] = _NO_EXCLUDE,
) -> CriticalPathResult:
    """Longest (critical) path through the dependency DAG.

    Folds the longest-path DP with the mapping to :class:`CriticalPathNode`.

    ``exclude`` is the set of ids the caller removes from path computation
    (completed tickets + cycle nodes). DP weight uses the 0->1 rule; ties break
    via :func:`lex_less`; dependency lists are deduped+sorted; roots are iterated
    in sorted id order. The returned :attr:`CriticalPathResult.minutes` is the
    half-up round of the summed DP **weights**.
    """

    # Weight by the 0 -> 1 rule; keep a node lookup for rendering.
    minutes_of: dict[str, int] = {}
    node_by_id: dict[str, TicketNode] = {}
    for n in tickets:
        if n.id in exclude:
            continue
        est = n.estimated_minutes
        minutes_of[n.id] = est if (est is not None and est > 0) else 1
        node_by_id[n.id] = n

    deps: dict[str, list[str]] = {tid: [] for tid in minutes_of}
    for e in edges:
        if e.ticket_id not in minutes_of or e.depends_on_id not in minutes_of:
            continue
        deps[e.ticket_id].append(e.depends_on_id)
    for k in deps:
        deps[k] = sorted(set(deps[k]))

    memo: dict[str, _Candidate] = {}

    def longest_to(node_id: str) -> _Candidate:
        cached = memo.get(node_id)
        if cached is not None:
            return cached

        own_minutes = minutes_of.get(node_id, 1)
        best = _Candidate(path=(), minutes=0)

        for dep_id in deps.get(node_id, []):
            sub = longest_to(dep_id)
            # Branches collapsed: each assigns `best = sub`, so the short-circuit
            # `or` chain is equivalent (the trailing clause is the
            # `best.path.length == 0 and sub.minutes >= 0` case).
            if (
                sub.minutes > best.minutes
                or (
                    sub.minutes == best.minutes
                    and len(best.path) > 0
                    and lex_less(sub.path, best.path)
                )
                or (len(best.path) == 0 and sub.minutes >= 0)
            ):
                best = sub

        result = _Candidate(path=(*best.path, node_id), minutes=best.minutes + own_minutes)
        memo[node_id] = result
        return result

    overall = _Candidate(path=(), minutes=0)
    for node_id in sorted(minutes_of):
        candidate = longest_to(node_id)
        # Branches collapsed (each assigns `overall = candidate`).
        # Note the trailing clause is `> 0`, NOT the inner loop's `>= 0`.
        if (
            candidate.minutes > overall.minutes
            or (
                candidate.minutes == overall.minutes
                and len(overall.path) > 0
                and lex_less(candidate.path, overall.path)
            )
            or (len(overall.path) == 0 and candidate.minutes > 0)
        ):
            overall = candidate

    path_nodes = tuple(_render_node(node_by_id[node_id]) for node_id in overall.path)
    return CriticalPathResult(
        path=path_nodes,
        length=len(path_nodes),
        minutes=round_half_up(overall.minutes),
    )


def _render_node(t: TicketNode) -> CriticalPathNode:
    """Build a display :class:`CriticalPathNode` with the build-tree guards.

    ``ticket_number`` defaults to 0 (``guardNumber``), ``title`` to the
    placeholder when missing/empty (``guardTitle``), ``estimated_minutes`` to 60
    only when the estimate is *missing* — a literal 0 renders as 0.
    """

    return CriticalPathNode(
        id=t.id,
        ticket_number=t.ticket_number if t.ticket_number is not None else 0,
        title=t.title if t.title else UNNUMBERED_PLACEHOLDER,
        estimated_minutes=t.estimated_minutes if t.estimated_minutes is not None else 60,
    )


def compute_max_blocker_depth(
    tickets: Iterable[TicketNode],
    edges: Iterable[DepEdge],
    *,
    exclude: frozenset[str] = _NO_EXCLUDE,
) -> int:
    """Longest edge-counted chain of *incomplete* blockers.

    A ``done`` dependency is satisfied: it is dropped and terminates the chain
    (depth 0). ``exclude`` (e.g. cycle nodes) is removed up front; a ``visiting``
    guard makes a residual cycle's back-edge contribute 0 so traversal always
    terminates. Returns only ``max_depth`` (the per-ticket depth map is unused by
    the summary).
    """

    complete_by_id: dict[str, bool] = {}
    for n in tickets:
        if n.id in exclude:
            continue
        complete_by_id[n.id] = n.status == "done"

    # Blocking deps = incomplete, non-excluded deps (a done dep does not block).
    blockers_of: dict[str, list[str]] = {tid: [] for tid in complete_by_id}
    for e in edges:
        if e.ticket_id not in complete_by_id or e.depends_on_id not in complete_by_id:
            continue
        if complete_by_id[e.depends_on_id]:
            continue
        blockers_of[e.ticket_id].append(e.depends_on_id)

    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(node_id: str) -> int:
        cached = memo.get(node_id)
        if cached is not None:
            return cached
        if complete_by_id.get(node_id):
            memo[node_id] = 0
            return 0
        if node_id in visiting:
            return 0  # residual-cycle guard
        visiting.add(node_id)

        best = 0
        for blocker in blockers_of.get(node_id, []):
            d = 1 + depth(blocker)
            if d > best:
                best = d

        visiting.discard(node_id)
        memo[node_id] = best
        return best

    max_depth = 0
    for node_id in complete_by_id:
        d = depth(node_id)
        if d > max_depth:
            max_depth = d
    return max_depth


@dataclass(slots=True)
class _TarjanFrame:
    """Mutable work-stack frame for the iterative Tarjan SCC (``{node, iter}``)."""

    node: str
    idx: int


def find_cycles(
    tickets_or_ids: Iterable[TicketNode | str],
    edges: Iterable[DepEdge],
) -> list[list[str]]:
    """All SCCs of size >= 2 plus self-loops, deterministically.

    Iterative Tarjan (explicit work stack, no recursion). Adjacency is
    deduped+sorted, nodes are visited in sorted id order, each returned cycle is
    sorted, and the result is sorted by ``cycle[0]``. ``tickets_or_ids`` may be
    :class:`TicketNode`s or bare id strings.
    """

    edge_list = list(edges)
    nodes: set[str] = {x if isinstance(x, str) else x.id for x in tickets_or_ids}
    for e in edge_list:
        nodes.add(e.ticket_id)
        nodes.add(e.depends_on_id)

    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for e in edge_list:
        if e.ticket_id == e.depends_on_id:
            continue
        adj[e.ticket_id].append(e.depends_on_id)
    for k in adj:
        adj[k] = sorted(set(adj[k]))

    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    sccs: list[list[str]] = []

    sorted_nodes = sorted(nodes)

    def strongconnect(v: str) -> None:
        nonlocal counter
        work_stack: list[_TarjanFrame] = [_TarjanFrame(node=v, idx=0)]
        index[v] = counter
        lowlink[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)

        while work_stack:
            frame = work_stack[-1]
            neighbors = adj.get(frame.node, [])

            if frame.idx < len(neighbors):
                w = neighbors[frame.idx]
                frame.idx += 1
                if w not in index:
                    index[w] = counter
                    lowlink[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work_stack.append(_TarjanFrame(node=w, idx=0))
                elif w in on_stack:
                    lowlink[frame.node] = min(lowlink[frame.node], index[w])
            else:
                if lowlink[frame.node] == index[frame.node]:
                    component: list[str] = []
                    while True:
                        w2 = stack.pop()
                        on_stack.discard(w2)
                        component.append(w2)
                        if w2 == frame.node:
                            break
                    sccs.append(component)
                work_stack.pop()
                if work_stack:
                    parent = work_stack[-1]
                    lowlink[parent.node] = min(lowlink[parent.node], lowlink[frame.node])

    for n in sorted_nodes:
        if n not in index:
            strongconnect(n)

    self_loops: set[str] = set()
    for e in edge_list:
        if e.ticket_id == e.depends_on_id:
            self_loops.add(e.ticket_id)

    cycles: list[list[str]] = []
    for component in sccs:
        if len(component) >= 2:
            cycles.append(sorted(component))
        elif len(component) == 1 and component[0] in self_loops:
            cycles.append([component[0]])

    cycles.sort(key=lambda c: c[0] if c else "")
    return cycles
