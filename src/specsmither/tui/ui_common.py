"""The shared presentation vocabulary for the Observatory (planner/02).

Glyph + Rich-style maps, the phase track, and the score bar — the one place the
TUI maps engine enum ``.value`` strings to how they look. Kept tiny and pure so
panels/screens stay declarative and snapshots stay stable.
"""

from __future__ import annotations

from specsmither.domain.enums import PlanningPhase

__all__ = [
    "PHASE_ORDER",
    "SESSION_STYLE",
    "SPEC_STYLE",
    "TICKET_GLYPH",
    "pct",
    "phase_track",
    "score_bar",
    "ticket_glyph",
]

#: Ticket status (``TicketStatus`` ``.value``) → (glyph, Rich style).
TICKET_GLYPH: dict[str, tuple[str, str]] = {
    "done": ("●", "green"),
    "active": ("◐", "yellow"),
    "ready": ("○", "cyan"),
    "pending": ("·", "dim"),
}

#: Spec status (``SpecStatus`` ``.value``) → Rich style.
SPEC_STYLE: dict[str, str] = {
    "draft": "dim",
    "planning": "yellow",
    "ready": "green",
    "in_progress": "cyan",
    "ready_for_review": "blue",
    "in_review": "blue",
    "reviewed": "bright_green",
    "done": "bright_green",
}

#: Planning-session status (``PlanningSessionStatus`` ``.value``) → Rich style.
SESSION_STYLE: dict[str, str] = {
    "active": "yellow",
    "awaiting_human_review": "bold magenta",
    "closed": "green",
}

#: The planning phases in machine order (drops nothing; the terminal sentinel
#: ``planned`` is last). The phase track renders ``PHASE_ORDER[:-1]`` (the six
#: actionable phases) — ``planned`` means "all phases cleared".
PHASE_ORDER: tuple[str, ...] = tuple(phase.value for phase in PlanningPhase)


def ticket_glyph(status: str) -> tuple[str, str]:
    """(glyph, style) for a ticket status; a safe fallback for an unknown value."""
    return TICKET_GLYPH.get(status, ("?", "white"))


def pct(value: float) -> int:
    """A 0..1 ratio as a whole-number percentage (half-up via ``round``)."""
    return round(value * 100)


def phase_track(current: str) -> str:
    """A compact ``●──◉──○`` track for the current planning phase.

    Phases before ``current`` are filled (``●``), the current is ``◉``, later are
    ``○``. ``planned`` (terminal) fills the whole track.
    """
    actionable = PHASE_ORDER[:-1]
    if current == PlanningPhase.PLANNED.value:
        cur_idx = len(actionable)
    elif current in actionable:
        cur_idx = actionable.index(current)
    else:
        cur_idx = 0
    parts = []
    for i in range(len(actionable)):
        if i < cur_idx:
            parts.append("●")
        elif i == cur_idx:
            parts.append("◉")
        else:
            parts.append("○")
    return "──".join(parts)


def score_bar(score: float, *, width: int = 16, threshold: float = 0.80) -> str:
    """A ``████░░░░ 62%`` bar for a 0..1 gate score (``threshold`` reserved for tint)."""
    filled = round(score * width)
    filled = max(0, min(filled, width))
    return "█" * filled + "░" * (width - filled) + f" {pct(score)}%"
