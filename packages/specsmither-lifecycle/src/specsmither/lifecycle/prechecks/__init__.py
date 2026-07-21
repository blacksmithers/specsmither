"""The 9 planning pre-checks — pure ``(…) -> Accepted | Denied`` guards.

Each check is a side-effect-free
function the APS / CPS pipeline (L4) runs in order; the first :class:`Denied`
short-circuits the verb into a denial envelope. The shared result union lives in
:mod:`.result` (:class:`Accepted` carries the ``rollback`` / ``auto_transition``
accept-side signals; :class:`Denied` carries ``code`` / ``message`` / ``context``
/ ``blockers``).

The checks (and what each consults):

* :func:`operation_allowed` — phase guard via ``classify_operation_call``;
  sets ``rollback`` for a *late* op (with the ``fieldDeclarations``-only exemption).
* :func:`spec_status_check` — the spec must be plannable for the verb; sets
  ``auto_transition`` for SPS on a ``draft`` spec.
* :func:`count_bounds` — min/max epics & tickets-per-epic from the validator config.
* :func:`cross_cut_references` — the crude substring referrer scan before
  ``delete_epic``.
* :func:`cascade_rules` — block an un-confirmed delete that would orphan
  dependency edges.
* :func:`blueprint_epic_ratio` — block ``delete_blueprint`` that breaches the ratio.
* :func:`schema_validate` — per-op payload shape (pydantic).
* :func:`validate_dependencies_batch` / :func:`run_dependencies_batch` — the
  ``create_dependencies`` batch dedup + cycle pre-check.
* :func:`gate_currently_passing` — the CPS gate (always re-validates; no TTL).
"""

from __future__ import annotations

from specsmither.lifecycle.prechecks.blueprint_epic_ratio import blueprint_epic_ratio
from specsmither.lifecycle.prechecks.blueprint_link_refs_exist import blueprint_link_refs_exist
from specsmither.lifecycle.prechecks.cascade_rules import cascade_rules
from specsmither.lifecycle.prechecks.content_shape_valid import content_shape_valid
from specsmither.lifecycle.prechecks.count_bounds import count_bounds
from specsmither.lifecycle.prechecks.cross_cut_references import cross_cut_references
from specsmither.lifecycle.prechecks.dependencies_batch import (
    BatchValidationCycle,
    BatchValidationOk,
    BatchValidationResult,
    DetectedCycle,
    run_dependencies_batch,
    validate_dependencies_batch,
)
from specsmither.lifecycle.prechecks.entity_refs_exist import entity_refs_exist
from specsmither.lifecycle.prechecks.enum_field_guards import enum_field_guards
from specsmither.lifecycle.prechecks.field_shape_soft_deny import field_shape_soft_deny
from specsmither.lifecycle.prechecks.gate_currently_passing import gate_currently_passing
from specsmither.lifecycle.prechecks.operation_allowed import operation_allowed
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult
from specsmither.lifecycle.prechecks.schema_validate import schema_validate
from specsmither.lifecycle.prechecks.spec_status_check import (
    SpecStatusVerb,
    recovery_hint_for_status,
    spec_status_check,
)

__all__ = [
    "Accepted",
    "BatchValidationCycle",
    "BatchValidationOk",
    "BatchValidationResult",
    "Denied",
    "DetectedCycle",
    "PrecheckResult",
    "SpecStatusVerb",
    "blueprint_epic_ratio",
    "blueprint_link_refs_exist",
    "cascade_rules",
    "content_shape_valid",
    "count_bounds",
    "cross_cut_references",
    "entity_refs_exist",
    "enum_field_guards",
    "field_shape_soft_deny",
    "gate_currently_passing",
    "operation_allowed",
    "recovery_hint_for_status",
    "run_dependencies_batch",
    "schema_validate",
    "spec_status_check",
    "validate_dependencies_batch",
]
