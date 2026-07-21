"""Shared helpers for the TUI tests (not collected — no ``test_`` prefix).

Builds an :class:`Observatory` over a throwaway user-global DB, seeds the canonical
gate-passing spec (the ``test_e2e_planning`` fixture), and drives the planning loop
to a terminal review so a Pilot test can exercise the final approve.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from specsmither.db.models import Project
from specsmither.operations.crud import (
    add_dependency,
    create_blueprint,
    create_epic,
    create_specification,
    create_ticket,
    link_blueprint_to_ticket,
)
from specsmither.tui.app import Observatory

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from specsmither.tui.data import Data

_SEED_PATH = Path(__file__).parent / "data" / "e2e_planning_seed.json"
SEED: dict = json.loads(_SEED_PATH.read_text(encoding="utf-8"))

PROJECT_ID = "01PROJECT0000000000000000A"

#: A fixed clock for deterministic report/snapshot rendering.
FIXED_CLOCK = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _materialize_brownfield(root: Path) -> None:
    """Write the brownfield files the seed's tickets consume but do not create.

    The validator's file-provenance check reads the workspace tree for grep evidence;
    the Observatory roots its prober at the workspace, so the seed's modified/referenced
    paths must exist there for the planning loop to clear cross_validation.
    """
    created: set[str] = set()
    consumed: set[str] = set()
    for epic in SEED["epics"]:
        for ticket in epic["tickets"]:
            created.update(ticket.get("filesToBeCreated", []) or [])
            for key in ("filesToBeModified", "filesToBeReferenced", "filesToBeDeleted"):
                consumed.update(ticket.get(key, []) or [])
    for rel in sorted(consumed - created):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")


def tui_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """An isolated user-global home + a workspace dir under ``tmp_path``."""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    _materialize_brownfield(workspace)
    env = {"SPECSMITHER_DB": str(home / "specsmither.db"), "SPECSMITHER_HOME": str(home)}
    return env, workspace


def make_app(tmp_path: Path, *, seed: bool = False) -> tuple[Observatory, str | None]:
    """Construct an Observatory over a tmp DB; optionally seed the canonical spec."""
    env, workspace = tui_env(tmp_path)
    app = Observatory(cwd=workspace, env=env, clock=FIXED_CLOCK)
    spec_id = seed_ready_spec(app.data.session_factory) if seed else None
    return app, spec_id


def seed_ready_spec(factory: sessionmaker[Session]) -> str:
    """Author the canonical gate-passing spec CLI-direct (mirrors test_e2e_planning)."""
    with factory.begin() as session:
        session.add(Project(id=PROJECT_ID, name="Canonical"))

    spec = create_specification(
        factory,
        project_id=PROJECT_ID,
        title=SEED["spec"]["title"],
        fields={k: v for k, v in SEED["spec"].items() if k != "title"},
    )
    ref_to_id: dict[str, str] = {}
    for epic in SEED["epics"]:
        created_epic = create_epic(
            factory,
            specification_id=spec.id,
            title=epic["title"],
            description=epic["description"],
            objective=epic["objective"],
            fields={
                k: v
                for k, v in epic.items()
                if k not in {"title", "description", "objective", "tickets"}
            },
        )
        for ticket in epic["tickets"]:
            created_ticket = create_ticket(
                factory,
                epic_id=created_epic.id,
                title=ticket["title"],
                fields={k: v for k, v in ticket.items() if k not in {"ref", "title"}},
            )
            ref_to_id[ticket["ref"]] = created_ticket.id

    blueprint_ids: dict[str, str] = {}
    for blueprint in SEED["blueprints"]:
        created_blueprint = create_blueprint(
            factory,
            specification_id=spec.id,
            title=blueprint["title"],
            category=blueprint["category"],
            fields={k: v for k, v in blueprint.items() if k not in {"ref", "title", "category"}},
        )
        blueprint_ids[blueprint["ref"]] = created_blueprint.id
    for link in SEED["blueprintLinks"]:
        link_blueprint_to_ticket(
            factory,
            ref_to_id[link["ticketRef"]],
            blueprint_ids[link["blueprintId"]],
            context=link.get("context"),
            section=link.get("section"),
        )
    for edge in SEED["dependencies"]:
        for depends_on in edge["dependsOn"]:
            add_dependency(factory, ref_to_id[edge["ticket"]], ref_to_id[depends_on])
    return spec.id


def tree_labels(node: object) -> list[str]:
    """Flatten a Textual Tree to its node label strings (for live-refresh assertions)."""
    labels: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        label = getattr(current, "label", None)
        if label is not None:
            labels.append(str(label))
        stack.extend(getattr(current, "children", []) or [])
    return labels


def drive_to_cross_validation(data: Data, spec_id: str) -> str:
    """Run the loop to ``cross_validation``, ACTIVE (where dependency ops are native)."""
    start = data.start_session(spec_id)
    sid = start.session_id
    assert sid is not None

    data.action(sid, "update_spec", {"fields": {"description": SEED["spec"]["description"]}})
    data.complete_session(sid)
    data.approve(sid)  # -> epic_decomposition
    data.complete_session(sid)
    data.approve(sid)  # -> epic_expansion
    epic = data.epics(spec_id)[0]
    data.action(sid, "update_epic", {"id": epic.id, "fields": {"description": epic.description}})
    data.complete_session(sid)
    data.approve(sid)  # -> ticket_decomposition
    data.complete_session(sid)
    data.approve(sid)  # -> ticket_expansion
    ticket = data.tickets_for_spec(spec_id)[0]
    data.action(sid, "update_ticket", {"id": ticket.id, "fields": {"description": ticket.description}})
    data.complete_session(sid)
    data.approve(sid)  # -> cross_validation (active)
    return sid


def drive_to_terminal_review(data: Data, spec_id: str) -> str:
    """Drive to ``cross_validation`` then park at ``awaiting_human_review`` (one approve from ready)."""
    sid = drive_to_cross_validation(data, spec_id)
    data.complete_session(sid)  # -> awaiting_human_review (terminal)
    return sid
