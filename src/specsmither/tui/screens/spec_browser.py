"""SpecBrowser (planner/02 #3): project → spec → epic → ticket tree + live detail.

A per-project tier (never flatten specs under one root). ``↑/↓`` previews a node in
the detail pane (no spec-context commit — the tree is not rebuilt, the cursor
stays put); ``Enter`` on a spec/epic/ticket commits the active spec (DAG / Planning
/ Work follow).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, Tree

from specsmither.tui.panels import (
    epic_panel,
    project_panel,
    root_hint,
    spec_panel,
    ticket_panel,
)
from specsmither.tui.ui_common import SPEC_STYLE, ticket_glyph

if TYPE_CHECKING:
    from specsmither.tui.app import Observatory

_NodeData = tuple[str, str]


class SpecBrowser(Horizontal):
    """The entity tree (left) + the live detail pane (right)."""

    def __init__(self) -> None:
        super().__init__()
        self._selected: _NodeData | None = None
        self._spec_of: dict[str, str] = {}  # epic_id / ticket_id -> spec_id

    def compose(self) -> ComposeResult:
        with Vertical(id="tree-pane"):
            yield Tree("specsmither", id="spec-tree")
        with VerticalScroll(id="detail-pane"):
            yield Static(id="detail", expand=True)

    def on_mount(self) -> None:
        self.reload()

    # -- tree ----------------------------------------------------------------- #

    @property
    def _app(self) -> Observatory:
        return cast("Observatory", self.app)

    def _build_tree(self) -> None:
        tree = self.query_one("#spec-tree", Tree)
        data = self._app.data
        current = self._app.current_spec_id
        tree.clear()
        tree.root.label = Text("specsmither", style="bold")
        tree.root.data = ("root", "")
        self._spec_of = {}
        snapshot = data.load_tree()
        single_project = len(snapshot.projects) == 1
        for ptree in snapshot.projects:
            project = ptree.project
            has_current = any(s.spec.id == current for s in ptree.specs)
            plabel = Text.assemble(
                (project.name + "  ", "bold"), (f"(project {project.id[:8]})", "dim")
            )
            pnode = tree.root.add(
                plabel, data=("project", project.id), expand=has_current or single_project
            )
            for stree in ptree.specs:
                spec = stree.spec
                status = spec.status.value
                slabel = Text.assemble(
                    (spec.title + "  ", "bold"), (f"[{status}]", SPEC_STYLE.get(status, "white"))
                )
                snode = pnode.add(slabel, data=("spec", spec.id), expand=(spec.id == current))
                crit, blocked = self._dag_marks(spec.id)
                for etree in stree.epics:
                    epic = etree.epic
                    self._spec_of[epic.id] = spec.id
                    elabel = Text.assemble(
                        (f"E{epic.epic_number}  " if epic.epic_number else "", "bold yellow"),
                        (epic.title, ""),
                        (f"  ({epic.progress}%)", "dim"),
                    )
                    enode = snode.add(elabel, data=("epic", epic.id), expand=True)
                    for ticket in etree.tickets:
                        self._spec_of[ticket.id] = spec.id
                        glyph, style = ticket_glyph(ticket.status.value)
                        num = f"T{ticket.ticket_number}  " if ticket.ticket_number else ""
                        tlabel = Text.assemble((f"{glyph} ", style), (num, "bold"), (ticket.title, ""))
                        if ticket.id in crit:
                            tlabel.append("  ★", style="magenta")
                        if ticket.id in blocked:
                            tlabel.append("  ⨯", style="red")
                        enode.add_leaf(tlabel, data=("ticket", ticket.id))
        tree.root.expand()

    def _dag_marks(self, spec_id: str) -> tuple[set[str], set[str]]:
        tree = self._app.data.dependency_tree(spec_id)
        if not tree:
            return set(), set()
        crit = {
            n.get("id")
            for n in (tree.get("critical_path") or [])
            if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        blocked = set(tree.get("summary", {}).get("blocked_tickets", []) or [])
        return cast("set[str]", crit), cast("set[str]", blocked)

    # -- selection ------------------------------------------------------------ #

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[_NodeData]) -> None:
        data = event.node.data
        if data is None:
            return
        self._selected = data
        self._render_detail()

    def on_tree_node_selected(self, event: Tree.NodeSelected[_NodeData]) -> None:
        data = event.node.data
        if data is None:
            return
        kind, ident = data
        self._selected = data
        if kind == "spec":
            self._app.set_current_spec(ident)
        elif kind in ("epic", "ticket"):
            spec_id = self._spec_of.get(ident)
            if spec_id is not None:
                self._app.set_current_spec(spec_id)
        self._render_detail()

    def selected_target(self) -> _NodeData | None:
        """What ``c``/``e``/``d`` act on (the highlighted node, or the live cursor)."""
        if self._selected is not None:
            return self._selected
        node = self.query_one("#spec-tree", Tree).cursor_node
        if node is not None and node.data is not None:
            return cast("_NodeData", node.data)
        return None

    def select_entity(self, kind: str, ident: str) -> None:
        """Point the detail pane at an entity (used after a create/edit)."""
        self._selected = (kind, ident)
        self._render_detail()

    def select_ticket(self, ticket_id: str) -> None:
        """Jump here (from Search) and show a ticket."""
        self._selected = ("ticket", ticket_id)
        self._render_detail()

    # -- detail --------------------------------------------------------------- #

    def _render_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        data = self._app.data
        sel = self._selected
        try:
            if sel is None:
                detail.update(root_hint())
                return
            kind, ident = sel
            if kind == "ticket":
                spec_id = self._spec_of.get(ident) or data.spec_id_of_ticket(ident)
                detail.update(ticket_panel(data.ticket(ident), data.dependency_tree(spec_id)))
            elif kind == "epic":
                detail.update(epic_panel(data.epic(ident), data.tickets(ident)))
            elif kind == "spec":
                detail.update(spec_panel(data.specification(ident), data.dependency_tree(ident)))
            elif kind == "project":
                detail.update(project_panel(data.project(ident), data.specifications(ident)))
            else:
                detail.update(root_hint())
        except Exception as exc:  # pragma: no cover - a stale selection must not crash
            detail.update(Text(f"(nothing to show: {exc})", style="dim"))

    def reload(self) -> None:
        self._build_tree()
        self._render_detail()
