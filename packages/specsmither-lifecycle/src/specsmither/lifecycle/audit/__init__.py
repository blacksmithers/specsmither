"""Audit row builders.

The **pure** factories that turn a verb's decision into the append-only audit row
payloads the M0 executor commits:

* :func:`build_action` — the ``planning_session_actions`` row (for ``ActionAppend``);
  stamps ``guidance_variant`` + ``findings_categories`` (the sole aggregate-fold
  stream source) + ``score`` + the audit columns.
* :func:`build_transition` — the ``planning_phase_transitions`` row (for
  ``RecordTransition``).
* :func:`map_path_to_operation` — maps a validator finding ``path`` to the
  ``PlanningOperationName`` that would fix it (drives recommended-moves guidance).
"""

from __future__ import annotations

from specsmither.lifecycle.audit.action_builder import build_action
from specsmither.lifecycle.audit.operation_mapper import MapperContext, map_path_to_operation
from specsmither.lifecycle.audit.transition_builder import build_transition

__all__ = [
    "MapperContext",
    "build_action",
    "build_transition",
    "map_path_to_operation",
]
