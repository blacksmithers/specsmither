"""Lifecycle-side rendering of the ``cycle_detected`` deny.

When the ``create_dependencies`` batch pre-check denies with ``cycle_detected``, the
bare "remove the offending edge" guidance names no specific edge and gives no basis
for the decision. This module turns each cyclic edge into an evidence packet the deny
renders so the agent can JUDGE which dependency is real — indicative, never
auto-cutting.

The structural analyzer owns the per-edge EVIDENCE (file-backing, epic relationship,
order, precedence hint); its only inputs are the cycle path + the spec, so it cannot
know which cyclic edges are in the incoming batch vs already persisted — and the
per-edge RECOVERY mechanism hinges on exactly that: a spurious edge's ORIGIN picks
omit-on-resubmit (``intra-batch``) vs ``delete_dependencies`` (``persisted``). This
module computes that origin tag (from the two edge lists in scope at the deny) and
renders the flow prose the deny surfaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from crucible.cross_validation.cycle_analysis import (
    CycleAnalysis,
    CycleEdgeEvidence,
    StructuralCycle,
    StructuralCycleEdge,
    analyze_cycle_edges,
)

from specsmither.lifecycle.i18n import t, text
from specsmither.lifecycle.prechecks.result import Denied

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from specsmither.lifecycle.ports import SpecDependencyEdge
    from specsmither.lifecycle.prechecks.dependencies_batch import BatchValidationCycle

__all__ = ["EdgeOrigin", "build_cycle_detected_denial", "tag_cycle_edge_origin"]

#: Where a cyclic edge comes from, relative to the ``create_dependencies`` batch check:
#:
#: - ``intra-batch`` — the edge is in the INCOMING batch (nothing is persisted yet, the
#:   whole batch is denied wholesale) → the recovery is RESUBMIT-omitting the edge;
#:   ``delete_dependencies`` would be a no-op.
#: - ``persisted`` — the edge already exists in the persisted graph and closes the loop
#:   through it → the recovery is ``delete_dependencies`` that edge.
EdgeOrigin = Literal["persisted", "intra-batch"]

#: The bare "remove the offending edge" deny shows at most this many cycles, with an
#: "…and N more" overflow line, to bound the deny size.
_MAX_CYCLES = 5


def _edge_key(from_id: str, to_id: str) -> str:
    return f"{from_id} {to_id}"


def tag_cycle_edge_origin(
    cycle_path: Sequence[StructuralCycleEdge],
    existing: Sequence[SpecDependencyEdge],
) -> list[EdgeOrigin]:
    """Per-edge origin, aligned to ``cycle_path`` (same length/order).

    An edge is ``persisted`` iff it appears in ``existing`` (the persisted dependency
    graph), else ``intra-batch``. The tag does NOT globally pick the verb: it is the
    per-edge MECHANISM once the EVIDENCE (file/order) has identified WHICH edge is
    spurious — deleting a persisted edge just because it is in the loop is wrong when
    the spurious edge is the new intra-batch one.
    """
    persisted = {_edge_key(e.from_ticket_id, e.to_ticket_id) for e in existing}
    return [
        "persisted" if _edge_key(e.from_ticket_id, e.to_ticket_id) in persisted else "intra-batch"
        for e in cycle_path
    ]


def _unique_loop_ids(cycle_path: Sequence[StructuralCycleEdge]) -> list[str]:
    """The distinct ticket ids in the loop, first-seen order (for ``get_ticket``)."""
    seen: set[str] = set()
    ids: list[str] = []
    for edge in cycle_path:
        for node in (edge.from_ticket_id, edge.to_ticket_id):
            if node not in seen:
                seen.add(node)
                ids.append(node)
    return ids


def _cycle_node_sequence(cycle_path: Sequence[StructuralCycleEdge]) -> str:
    """The node sequence ``a → b → a``."""
    if not cycle_path:
        return ""
    nodes = [cycle_path[0].from_ticket_id, *(e.to_ticket_id for e in cycle_path)]
    return " → ".join(nodes)


def _order_clause(edge: CycleEdgeEvidence, language: str) -> str:
    """The epic/order clause: ``same epic`` / ``cross-epic``, with the order when it
    distinguishes the two sides."""
    scope_key = (
        "cycle.scope.same-epic"
        if edge.epic_relationship == "same-epic"
        else "cycle.scope.cross-epic"
    )
    scope = text(language, scope_key)
    if edge.from_order is not None and edge.to_order is not None and edge.from_order != edge.to_order:
        return t(
            language,
            "cycle.order.detailed",
            {
                "scope": scope,
                "orderBasis": edge.order_basis,
                "fromId": edge.from_ticket_id,
                "fromOrder": edge.from_order,
                "toId": edge.to_ticket_id,
                "toOrder": edge.to_order,
            },
        )
    return scope


def _format_edge_evidence(edge: CycleEdgeEvidence, origin: EdgeOrigin, language: str) -> str:
    """One per-edge evidence line: file / epic / order / hint / origin."""
    if edge.file_backed:
        file_clause = t(
            language,
            "cycle.file.backed",
            {
                "fromId": edge.from_ticket_id,
                "files": ", ".join(edge.justifying_files),
                "toId": edge.to_ticket_id,
            },
        )
    else:
        file_clause = text(language, "cycle.file.none")
    return t(
        language,
        "cycle.edge.evidence",
        {
            "fromId": edge.from_ticket_id,
            "toId": edge.to_ticket_id,
            "fileClause": file_clause,
            "orderClause": _order_clause(edge, language),
            "hintClause": text(language, f"cycle.hint.{edge.precedence_hint}"),
            "originTag": text(language, f"cycle.origin.{origin}"),
        },
    )


def _recovery_for(analysis: CycleAnalysis, origins: list[EdgeOrigin], language: str) -> str:
    """The 2-D recovery dispatch (cycle shape × the spurious edge's origin).

    ``get_ticket`` leads every path. (1) ``all-file-backed`` → ``update_ticket`` (a
    contradictory FILE dependency; reshape the files, don't drop an edge). (2) otherwise
    the EVIDENCE picks the spurious edge (the ``reverse``-hint direction), then THAT
    edge's ORIGIN picks the mechanism: intra-batch → RESUBMIT-omitting; persisted →
    ``delete_dependencies``. Ambiguous loops orient to the tickets.
    """
    get_ticket = t(
        language,
        "cycle.getTicket",
        {"ids": " ".join(_unique_loop_ids(analysis.cycle_path))},
    )

    if analysis.shape_tag == "all-file-backed":
        return t(language, "cycle.recovery.all-file-backed", {"getTicket": get_ticket})

    spurious = [(idx, edge) for idx, edge in enumerate(analysis.edges) if edge.precedence_hint == "reverse"]
    if len(spurious) == 1:
        idx, edge = spurious[0]
        label = f"`{edge.from_ticket_id} → {edge.to_ticket_id}`"
        origin = origins[idx] if idx < len(origins) else "intra-batch"
        key = (
            "cycle.recovery.spuriousPersisted"
            if origin == "persisted"
            else "cycle.recovery.spuriousIntraBatch"
        )
        return t(language, key, {"getTicket": get_ticket, "label": label})

    mechanism_key = (
        "cycle.recovery.mechanism.persisted"
        if "persisted" in origins
        else "cycle.recovery.mechanism.intra-batch"
    )
    order_note = (
        text(language, "cycle.recovery.orderNote")
        if analysis.shape_tag == "none-file-backed"
        else ""
    )
    return t(
        language,
        "cycle.recovery.ambiguous",
        {
            "getTicket": get_ticket,
            "orderNote": order_note,
            "mechanism": text(language, mechanism_key),
        },
    )


def format_cycle_analysis(
    analyses: Sequence[CycleAnalysis],
    existing: Sequence[SpecDependencyEdge],
    language: str,
) -> str:
    """Render the indicative evidence packet for the ``cycle_detected`` deny.

    Per cycle: the node sequence + shape tag, the per-edge evidence (file / epic / order
    / hint / origin), and the 2-D recovery recommendation. Shows at most
    :data:`_MAX_CYCLES` cycles with an "…and N more" overflow.
    """
    blocks: list[str] = []
    for i, analysis in enumerate(analyses[:_MAX_CYCLES]):
        origins = tag_cycle_edge_origin(analysis.cycle_path, existing)
        lines = [
            t(
                language,
                "cycle.block.header",
                {
                    "n": i + 1,
                    "nodeSequence": _cycle_node_sequence(analysis.cycle_path),
                    "shapeLabel": text(language, f"cycle.shape.{analysis.shape_tag}"),
                },
            )
        ]
        for idx, edge in enumerate(analysis.edges):
            origin = origins[idx] if idx < len(origins) else "intra-batch"
            lines.append(
                t(
                    language,
                    "cycle.block.edge",
                    {"evidence": _format_edge_evidence(edge, origin, language)},
                )
            )
        lines.append(
            t(
                language,
                "cycle.block.recovery",
                {"recovery": _recovery_for(analysis, origins, language)},
            )
        )
        blocks.append("\n".join(lines))
    if len(analyses) > _MAX_CYCLES:
        blocks.append(t(language, "cycle.overflow", {"count": len(analyses) - _MAX_CYCLES}))
    return "\n\n".join(blocks)


def _cycle_path_arrow(cycle_path: Sequence[SpecDependencyEdge]) -> str:
    """Render a cycle's edge list as an arrow chain (``t1 -> t2 -> t1``) for the audit
    detail bag."""
    if not cycle_path:
        return ""
    nodes = [cycle_path[0].from_ticket_id, *(e.to_ticket_id for e in cycle_path)]
    return " -> ".join(nodes)


def build_cycle_detected_denial(
    batch: BatchValidationCycle,
    spec: Mapping[str, Any],
    existing: Sequence[SpecDependencyEdge],
    language: str,
) -> Denied:
    """Enrich the ``cycle_detected`` deny with the per-edge evidence + recovery packet.

    ``spec`` is the crucible wire dict of the (projected) spec; ``create_dependencies``
    only ADDS edges, so the pre-mutation spec is the projected spec for this file/order
    analysis. ``existing`` is the persisted dependency graph — it drives the
    intra-batch-vs-persisted origin tag the analyzer cannot know. The node sequence is
    also kept in the audit ``context`` for observability (the deny prose renders the
    rich analysis, not the raw one-liner).
    """
    structural_cycles = [
        StructuralCycle(
            cycle_path=[
                StructuralCycleEdge(from_ticket_id=e.from_ticket_id, to_ticket_id=e.to_ticket_id)
                for e in cycle.cycle_path
            ]
        )
        for cycle in batch.cycles
    ]
    analyses = analyze_cycle_edges(structural_cycles, dict(spec))
    cycle_analysis = format_cycle_analysis(analyses, existing, language)
    message = t(
        language,
        "cycle.deny.frame",
        {"cycleCount": len(batch.cycles), "cycleAnalysis": cycle_analysis},
    )
    return Denied(
        code="cycle_detected",
        message=message,
        context={
            "cycle_count": len(batch.cycles),
            "dropped_intra_batch": len(batch.dropped_intra_batch),
            "cycle_paths": [_cycle_path_arrow(c.cycle_path) for c in batch.cycles],
        },
    )
