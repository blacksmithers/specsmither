"""The five Observatory tab screens (planner/02 #3–#7).

Each screen is a self-contained Textual widget with a ``reload()`` the app calls on
every mutation/refresh; all reads flow through ``self.app.data`` (the seam).
"""

from __future__ import annotations

from specsmither.tui.screens.dag_view import DagView
from specsmither.tui.screens.planning_monitor import PlanningMonitor
from specsmither.tui.screens.search import SearchView
from specsmither.tui.screens.spec_browser import SpecBrowser
from specsmither.tui.screens.work_progress import WorkProgress

__all__ = ["DagView", "PlanningMonitor", "SearchView", "SpecBrowser", "WorkProgress"]
