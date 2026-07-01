"""Rich renderable builders for the Observatory (planner/02).

Pure functions: given engine records + the materialized dependency tree, return a
Rich renderable the screens drop into ``Static.update(...)`` (so any mutation +
refresh re-renders live). All graph facts (depends-on / blocks / blocked-by /
critical-path) are derived from the cached ``dependency_tree`` dict — the same
single source of truth the queries serve — so the human and agent views agree.
"""

from __future__ import annotations

from typing import Any

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from specsmither.domain.records import EpicRecord, SpecificationRecord, TicketRecord
from specsmither.tui.data import ContextInfo, LogRow, SessionView
from specsmither.tui.ui_common import (
    SESSION_STYLE,
    SPEC_STYLE,
    phase_track,
    score_bar,
    ticket_glyph,
)

__all__ = [
    "context_line",
    "dag_panel",
    "epic_panel",
    "init_plan_panel",
    "planning_panel",
    "project_panel",
    "root_hint",
    "spec_panel",
    "ticket_panel",
    "work_panel",
]

_THRESHOLD_PHASES = {"planning_spec", "epic_expansion", "ticket_expansion"}


# --------------------------------------------------------------------------- #
# dependency-tree helpers (derive the mock's t.depends_on / blocks / …)        #
# --------------------------------------------------------------------------- #


def _nodes(tree: dict[str, Any] | None) -> dict[str, Any]:
    if not tree:
        return {}
    nodes = tree.get("tickets")
    return nodes if isinstance(nodes, dict) else {}


def _tlabel(node: Any) -> str:
    """A friendly ``E2-T3`` label for a tree ticket node (falls back to its number/id)."""
    if not isinstance(node, dict):
        return "?"
    epic = node.get("epic_number")
    num = node.get("ticket_number")
    if epic is not None and num is not None:
        return f"E{epic}-T{num}"
    if num is not None:
        return f"T{num}"
    ident = node.get("id", "?")
    return str(ident)[:8]


def _labels(tree: dict[str, Any] | None, ids: list[str]) -> list[str]:
    nodes = _nodes(tree)
    return [_tlabel(nodes.get(i, {"id": i})) for i in ids]


def _critical_ids(tree: dict[str, Any] | None) -> set[str]:
    if not tree:
        return set()
    path = tree.get("critical_path") or []
    out = {n.get("id") for n in path if isinstance(n, dict) and n.get("id")}
    return {i for i in out if isinstance(i, str)}


def _facts(tree: dict[str, Any] | None, ticket_id: str) -> dict[str, Any]:
    """Derive a ticket's (depends_on, blocks, blocked_by, on_critical_path, blocked)."""
    nodes = _nodes(tree)
    node = nodes.get(ticket_id)
    if not isinstance(node, dict):
        return {"depends_on": [], "blocks": [], "blocked_by": [], "critical": False, "blocked": False}
    return {
        "depends_on": _labels(tree, list(node.get("dependencies") or [])),
        "blocks": _labels(tree, list(node.get("blocks") or [])),
        "blocked_by": _labels(tree, list(node.get("unsatisfied_deps") or [])),
        "critical": ticket_id in _critical_ids(tree),
        "blocked": bool(node.get("unsatisfied_deps")),
    }


# --------------------------------------------------------------------------- #
# entity detail panels                                                         #
# --------------------------------------------------------------------------- #


def _numbered(title: str, items: list[str], empty: str = "—") -> Panel:
    if not items:
        return Panel(Text(empty, style="dim"), title=title)
    body = Text()
    for i, item in enumerate(items):
        body.append(f"{i + 1}. ", style="dim")
        body.append(item + ("\n" if i < len(items) - 1 else ""))
    return Panel(body, title=title)


def _ac_lines(ticket: TicketRecord) -> list[str]:
    return [f"{ac.given} → {ac.when} ⇒ {ac.then}" for ac in ticket.acceptance_criteria]


def _file_lines(ticket: TicketRecord) -> list[str]:
    out: list[str] = []
    out += [f"＋ {f}" for f in ticket.files_to_be_created]
    out += [f"～ {f}" for f in ticket.files_to_be_modified]
    out += [f"− {f}" for f in ticket.files_to_be_deleted]
    out += [f"→ {f}" for f in ticket.files_to_be_referenced]
    return out


def ticket_panel(ticket: TicketRecord, tree: dict[str, Any] | None) -> Group:
    """The ticket detail pane (description / AC / steps / files / dependencies)."""
    status = ticket.status.value
    glyph, style = ticket_glyph(status)
    facts = _facts(tree, ticket.id)
    num = f"T{ticket.ticket_number}" if ticket.ticket_number else "ticket"
    header = Text.assemble(
        (f"{glyph} ", style),
        (num, "bold"),
        (f"  {ticket.title}", ""),
        (f"   [{status}]", style),
    )
    if facts["critical"]:
        header.append("   ★critical-path", style="magenta")

    ac = _ac_lines(ticket)
    steps = [step.text for step in ticket.implementation_steps]
    files = _file_lines(ticket)

    deps = Text()
    deps.append("depends on: ", style="dim")
    deps.append(", ".join(facts["depends_on"]) or "—")
    deps.append("\nblocks:     ", style="dim")
    deps.append(", ".join(facts["blocks"]) or "—")
    if facts["blocked_by"]:
        deps.append("\nblocked by: ", style="dim")
        deps.append(", ".join(facts["blocked_by"]), style="red")

    return Group(
        Panel(Text(ticket.description or "—"), title=str(header), border_style=style),
        _numbered(f"acceptance criteria  ({len(ac)})", ac, "⚠ none — gate-blocking"),
        Columns([_numbered("steps", steps), _numbered("files", files)], expand=True),
        Panel(deps, title="dependencies"),
        Text.from_markup(
            "\n[dim]e edit · c new ticket · d delete · deps edited in the ticket form · "
            "Planning tab drives the handover[/]",
            justify="center",
        ),
    )


def _spec_metrics(spec: SpecificationRecord, tree: dict[str, Any] | None) -> Text:
    body = Text()
    body.append((spec.description or "—") + "\n\n")
    body.append("epics    ", style="dim")
    body.append(f"{spec.epic_count}\n")
    body.append("tickets  ", style="dim")
    body.append(f"{spec.ticket_count}\n")
    body.append("progress ", style="dim")
    body.append(f"{spec.progress}%\n")
    if tree:
        summary = tree.get("summary", {})
        ready = len(summary.get("ready_tickets", []) or [])
        blocked = len(summary.get("blocked_tickets", []) or [])
        crit = summary.get("critical_path_minutes")
        body.append("ready    ", style="dim")
        body.append(f"{ready}", style="cyan")
        body.append("   blocked ", style="dim")
        body.append(f"{blocked}\n", style="red")
        raw_ids = [n.get("id") for n in (tree.get("critical_path") or []) if isinstance(n, dict)]
        path_ids = _labels(tree, [i for i in raw_ids if isinstance(i, str)])
        body.append("critical ", style="dim")
        body.append(" → ".join(path_ids) or "—", style="magenta")
        if crit is not None:
            body.append(f"  ({crit} min)", style="dim")
    else:
        body.append("\n[dim]dependency tree not yet materialized — apply a mutation[/]")
    return body


def spec_panel(spec: SpecificationRecord, tree: dict[str, Any] | None) -> Group:
    """The spec detail pane (description + epic/ticket/ready/blocked + critical path)."""
    status = spec.status.value
    head = Text.assemble(
        (spec.title, "bold"),
        (f"   [{status}]", SPEC_STYLE.get(status, "white")),
    )
    return Group(
        Panel(
            _spec_metrics(spec, tree),
            title=str(head),
            border_style=SPEC_STYLE.get(status, "white"),
        ),
        Text.from_markup(
            "\n[dim]select a ticket on the left for detail · e edit spec · c new epic[/]",
            justify="center",
        ),
    )


def epic_panel(epic: EpicRecord, tickets: list[TicketRecord]) -> Group:
    """The epic detail pane (objective + ticket roster)."""
    head = Text.assemble(
        (f"E{epic.epic_number}  " if epic.epic_number else "", "bold yellow"),
        (epic.title, "bold"),
        (f"   [{epic.status.value}]", "yellow"),
    )
    body = Text()
    body.append("objective  ", style="dim")
    body.append((epic.objective or "—") + "\n")
    body.append("progress   ", style="dim")
    body.append(f"{epic.progress}%   ")
    body.append("tickets ", style="dim")
    body.append(f"{epic.ticket_count}\n\n")
    if not tickets:
        body.append("no tickets yet — press c to add one", style="dim")
    for ticket in tickets:
        glyph, style = ticket_glyph(ticket.status.value)
        body.append(f"{glyph} ", style=style)
        body.append(f"T{ticket.ticket_number}  " if ticket.ticket_number else "")
        body.append(f"{ticket.title}\n")
    return Group(
        Panel(body, title=str(head), border_style="yellow"),
        Text.from_markup(
            "\n[dim]c new ticket · e edit epic · select a ticket for detail[/]", justify="center"
        ),
    )


def project_panel(project: Any, specs: list[SpecificationRecord]) -> Group:
    """The project detail pane (spec roster)."""
    head = Text.assemble((project.name, "bold"), ("  (project)", "dim"))
    body = Text()
    if not specs:
        body.append("no specifications yet — press c to create one", style="dim")
    for spec in specs:
        status = spec.status.value
        body.append(f"{spec.title}  ", style="bold")
        body.append(f"[{status}]\n", style=SPEC_STYLE.get(status, "white"))
    return Group(
        Panel(body, title=str(head), border_style="blue"),
        Text.from_markup(
            "\n[dim]c new spec · select a spec for detail / e to edit[/]", justify="center"
        ),
    )


def root_hint() -> Text:
    return Text.from_markup(
        "[dim]projects → specs → epics → tickets\n\n"
        "c new project (on root) · new spec (on a project)[/]",
        justify="center",
    )


# --------------------------------------------------------------------------- #
# DAG panel                                                                    #
# --------------------------------------------------------------------------- #


def dag_panel(
    spec: SpecificationRecord, tickets: list[TicketRecord], tree: dict[str, Any] | None
) -> Group:
    """Critical path · cycles · the dependency-edge table (ready/blocked split)."""
    if tree is None:
        return Group(
            Panel(
                Text.from_markup(
                    "Dependency tree not yet materialized.\n"
                    "Apply a mutation (the recompute worklist builds it).",
                    justify="center",
                ),
                title=f"DAG · {spec.title}",
                border_style="dim",
            )
        )

    summary = tree.get("summary", {})
    path_ids = [n.get("id") for n in (tree.get("critical_path") or []) if isinstance(n, dict)]
    cp = Text(" → ".join(_labels(tree, [i for i in path_ids if isinstance(i, str)])), style="magenta")
    cp.append(
        f"\n{summary.get('critical_path_minutes', 0)} min  ({len(path_ids)} tickets)", style="dim"
    )
    items: list[RenderableType] = [
        Panel(cp, title=f"critical path · {spec.title}", border_style="magenta")
    ]

    cycles = tree.get("cycles") or []
    if cycles or summary.get("has_circular_deps"):
        members: list[str] = []
        for cyc in cycles:
            if isinstance(cyc, dict):
                members += _labels(tree, [i for i in (cyc.get("cycle") or []) if isinstance(i, str)])
        items.append(
            Panel(
                Text(", ".join(members) or "(cycle present)", style="red"),
                title="⚠ dependency cycle among",
                border_style="red",
            )
        )

    ready_ids = set(summary.get("ready_tickets", []) or [])
    blocked_ids = set(summary.get("blocked_tickets", []) or [])
    crit = _critical_ids(tree)
    nodes = _nodes(tree)

    table = Table(title="dependency edges", expand=True)
    table.add_column("ticket", style="bold", no_wrap=True)
    table.add_column("status")
    table.add_column("depends on")
    table.add_column("blocks")
    table.add_column("state")
    for ticket in tickets:
        node = nodes.get(ticket.id, {})
        status = ticket.status.value
        glyph, style = ticket_glyph(status)
        if ticket.id in ready_ids:
            state = Text("ready", style="cyan")
        elif ticket.id in blocked_ids:
            state = Text("blocked", style="red")
        else:
            state = Text(status, style="dim")
        label = Text.assemble((f"{glyph} ", style), (_tlabel(node) if node else ticket.id[:8], "bold"))
        if ticket.id in crit:
            label.append(" ★", style="magenta")
        deps = ", ".join(_labels(tree, list(node.get("dependencies") or []))) if node else ""
        blocks = ", ".join(_labels(tree, list(node.get("blocks") or []))) if node else ""
        table.add_row(label, Text(status, style=style), deps or "—", blocks or "—", state)
    items.append(table)
    return Group(*items)


# --------------------------------------------------------------------------- #
# planning monitor                                                             #
# --------------------------------------------------------------------------- #


def _log_table(rows: list[LogRow]) -> Table:
    table = Table(
        title=f"action log  ({len(rows)} entries · newest first)", expand=True
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("actor")
    table.add_column("phase")
    table.add_column("verb")
    table.add_column("detail")
    for row in rows:
        if row.kind == "transition" and row.transition is not None:
            tr = row.transition
            table.add_row(
                str(row.seq),
                Text(tr.actor or "—", style="blue"),
                Text(f"{tr.from_phase or '—'} → {tr.to_phase or '—'}", style="blue"),
                Text("transition", style="blue"),
                Text(tr.trigger or "", style="blue dim"),
            )
        elif row.action is not None:
            ac = row.action
            actor = ac.actor or "agent"
            detail = ac.guidance_variant or ac.outcome or ""
            if ac.score is not None:
                detail = f"{detail}  ({score_bar(ac.score, width=8)})"
            table.add_row(
                str(row.seq),
                Text(actor, style="magenta" if actor == "human" else "cyan"),
                Text(ac.phase or "—"),
                Text(ac.operation or "—"),
                Text(detail, style="red" if ac.outcome == "denied" else ""),
            )
    return table


def planning_panel(
    spec: SpecificationRecord | None,
    session: SessionView | None,
    rows: list[LogRow],
) -> Group:
    """Phase / score / gate panel + the interleaved agent action log."""
    if spec is None:
        return Group(
            Panel(
                Text.from_markup(
                    "No specification selected.\nPick a spec in the Browser first.",
                    justify="center",
                ),
                title="planning monitor",
                border_style="dim",
            )
        )
    if session is None:
        return Group(
            Panel(
                Text.from_markup(
                    f"No active planning session for [bold]{spec.title}[/].\n"
                    "Press [bold]n[/] (or the Start button) to begin planning a draft spec.",
                    justify="center",
                ),
                title="planning monitor",
                border_style="dim",
            )
        )

    head = Group(
        Text.assemble(("spec    ", "dim"), (spec.title, "bold")),
        Text.assemble(("status  ", "dim"), (session.status, SESSION_STYLE.get(session.status, "white"))),
        Text.assemble(("phase   ", "dim"), (session.current_phase, "bold")),
        Text.assemble(("        ", ""), (phase_track(session.current_phase), "")),
    )
    items: list[RenderableType] = [
        Panel(head, title="planning monitor", border_style=SESSION_STYLE.get(session.status, "white"))
    ]

    if session.status == "awaiting_human_review":
        items.append(
            Panel(
                Text.from_markup(
                    "⤷ awaiting human handover — press [bold]a[/] approve · [bold]j[/] reject",
                    justify="center",
                ),
                border_style="magenta",
            )
        )

    if session.last_gate_result is not None:
        gate = Text.assemble(
            ("last gate  ", "dim"),
            (
                "PASS" if session.last_gate_result == "pass" else "FAIL",
                "green" if session.last_gate_result == "pass" else "red",
            ),
        )
        if session.current_phase in _THRESHOLD_PHASES and session.last_score is not None:
            gate.append("\nthreshold  ", style="dim")
            gate.append(score_bar(session.last_score))
        items.append(gate)

    feedback = session.pending_human_feedback
    if isinstance(feedback, dict) and feedback.get("content"):
        items.append(
            Panel(
                Text(str(feedback["content"])),
                title="pending human feedback (consumed on next poll)",
                border_style="magenta",
            )
        )

    items.append(_log_table(rows))
    return Group(*items)


# --------------------------------------------------------------------------- #
# work progress (0.2.0 preview) + init plan + context line                     #
# --------------------------------------------------------------------------- #


def work_panel(spec: SpecificationRecord, tickets: list[TicketRecord]) -> Group:
    """The 4-dimension assay view — a read-only 0.2.0 preview (gate lands in planner/03)."""
    banner = Panel(
        Text(
            "WorkProgress is a 0.2.0 surface — the work gate lives in `assay` (planner/03).\n"
            "Shown here as the shape the screen will take, over this spec's tickets.",
            justify="center",
            style="dim",
        ),
        title="⧗ preview (work lifecycle = 0.2.0)",
        border_style="yellow",
    )
    table = Table(title=f"assay dimensions · {spec.title}", expand=True)
    table.add_column("ticket", style="bold", no_wrap=True)
    table.add_column("status")
    table.add_column("files\nchanged", justify="center")
    table.add_column("ac\nverified", justify="center")
    table.add_column("tests\njustified", justify="center")
    table.add_column("steps\ndone", justify="center")
    for ticket in tickets:
        status = ticket.status.value
        glyph, style = ticket_glyph(status)
        label = f"T{ticket.ticket_number}" if ticket.ticket_number else ticket.id[:6]
        table.add_row(
            Text.assemble((f"{glyph} ", style), (label, "bold")),
            Text(status, style=style),
            _assay_cell(status), _assay_cell(status), _assay_cell(status), _assay_cell(status),
        )
    return Group(banner, table)


def _assay_cell(status: str) -> Text:
    """One assay-dimension cell glyph for the 0.2.0 WorkProgress preview."""
    if status == "done":
        return Text("✓", style="green")
    if status == "active":
        return Text("◐", style="yellow")
    return Text("·", style="dim")


def init_plan_panel(items: list[tuple[str, str, str]]) -> Table:
    """``items``: list of (path, note, status) where status ∈ created|exists|pending."""
    table = Table(expand=True)
    table.add_column("", width=2)
    table.add_column("path", style="bold")
    table.add_column("note", style="dim")
    glyphs = {
        "created": ("✚", "green"),
        "exists": ("=", "dim"),
        "pending": ("·", "dim"),
        "repaired": ("↻", "cyan"),
        "skipped": ("!", "yellow"),
    }
    for path, note, status in items:
        glyph, style = glyphs.get(status, ("·", "dim"))
        table.add_row(Text(glyph, style=style), path, note)
    return table


def context_line(ctx: ContextInfo) -> Text:
    """The always-visible ContextBar line (active spec · session · ready/blocked)."""
    line = Text()
    if ctx.project is not None:
        line.append(" ⬡ ", style="blue")
        line.append(f"{ctx.project.name}", style="bold")
        line.append(" · ", style="dim")
    if ctx.spec is not None:
        status = ctx.spec.status.value
        line.append(f"{ctx.spec.title} ", style="bold")
        line.append(f"[{status}]", style=SPEC_STYLE.get(status, "white"))
    if ctx.session is not None:
        line.append("   session ", style="dim")
        line.append(ctx.session.status, style=SESSION_STYLE.get(ctx.session.status, "white"))
        line.append(f" @ {ctx.session.current_phase}", style="dim")
    line.append("   worklist ", style="dim")
    line.append(f"{ctx.ready}▸ready", style="cyan")
    line.append(" ", style="dim")
    line.append(f"{ctx.blocked}⨯blocked", style="red")
    if ctx.project is None:
        line.append("   (uninitialized — press i)", style="yellow")
    return line
