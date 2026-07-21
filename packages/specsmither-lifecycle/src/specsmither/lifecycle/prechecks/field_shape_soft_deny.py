"""Pre-check: soft-deny content-array items whose shape the read boundary rejects.

Pure. Distinct from the write-boundary id/order filler (``write_plan.ensure_item_ids``,
which mints the SCHEMA-internal id/order) and from the top-level enum write-guard, this catches
CONTENT offenders nested inside the ``a.json()`` arrays of ``update_epic`` / ``update_spec`` that
the crucible sub-models reject on read (so the whole array would silently vanish):

* ``apiContracts[].type`` that is not a valid :class:`ApiContractType` even after case-folding
  (``REST`` -> ``rest`` is normalised at the write boundary; ``SOAP`` is a genuine offender);
* ``fileStructures[]`` / ``folderStructures[]`` items missing the required ``content``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["VALID_API_CONTRACT_TYPES", "field_shape_soft_deny"]

#: The valid ``ApiContract.type`` vocabulary (crucible ``ApiContractType``). The write
#: boundary lower-cases the value; a value still off this set after folding is denied.
VALID_API_CONTRACT_TYPES = frozenset({"rest", "graphql", "rpc", "event", "cli"})

_STRUCTURE_FIELDS = ("fileStructures", "folderStructures")


def field_shape_soft_deny(
    op: PlanningOperationName,
    payload: Mapping[str, Any],
) -> PrecheckResult:
    """Soft-deny an update carrying an off-enum apiContract type or a content-less structure."""
    if op not in ("update_epic", "update_spec"):
        return Accepted()
    fields = payload.get("fields")
    if not isinstance(fields, Mapping):
        return Accepted()

    api_contracts = fields.get("apiContracts")
    if isinstance(api_contracts, list):
        for contract in api_contracts:
            if not isinstance(contract, Mapping):
                continue
            raw_type = contract.get("type")
            if isinstance(raw_type, str) and raw_type.lower() not in VALID_API_CONTRACT_TYPES:
                return Denied(
                    code="invalid_field_shape",
                    message=(
                        f"apiContracts[].type '{raw_type}' is not a valid contract type. "
                        f"Use one of: {', '.join(sorted(VALID_API_CONTRACT_TYPES))}."
                    ),
                    context={"field": "apiContracts.type", "value": raw_type},
                )

    for name in _STRUCTURE_FIELDS:
        items = fields.get(name)
        if isinstance(items, list):
            for i, item in enumerate(items):
                if isinstance(item, Mapping) and not (item.get("content") or "").strip():
                    return Denied(
                        code="invalid_field_shape",
                        message=(
                            f"{name}[{i}] is missing the required 'content'. Every "
                            "file/folder structure item must carry a non-empty content string."
                        ),
                        context={"field": f"{name}.content", "index": i},
                    )

    return Accepted()
