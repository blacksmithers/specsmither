"""Pre-check: reject an ``update_*`` ``fields`` map that carries an unrecognized key.

The per-op :func:`~specsmither.lifecycle.prechecks.schema_validate.schema_validate`
models type ``update_*`` payloads' ``fields`` as ``dict[str, Any]`` — any dict passes,
because the field *values* are re-validated when crucible re-parses the projected spec.
That leaves a silent-no-op hole: a key *inside* ``fields`` that is not a real attribute
of the target entity — a typo (``gaols``), a double-wrap (``{fields: {fields: {…}}}``),
or content mis-nested under the wrong name — is snake-cased and ``setattr``'d onto the
``extra='allow'`` crucible model (:func:`operations_projector._assign`), landing as a
harmless stray attribute while the real field stays untouched. The mutation "succeeds"
but writes nothing, the gate never moves, and the agent gets no corrective signal — the
exact loop that trapped a spec at a stuck score.

This gate closes the hole: for the four ``update_*`` ops it snake-cases each ``fields``
key (the same normalization :func:`operations_projector._snake_fields` applies before the
write) and denies any key that is not a declared field of the target crucible model, so a
mis-nested payload becomes a :class:`Denied` (``invalid_payload``) that names the offending
key instead of a phantom success.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from crucible.models import Blueprint, Epic, Specification, Ticket
from pydantic import BaseModel

from specsmither.lifecycle.operations_registry import PlanningOperationName
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult
from specsmither.lifecycle.write_plan_types import to_snake

__all__ = ["unknown_field_key"]


def _known(model: type[BaseModel]) -> frozenset[str]:
    return frozenset(model.model_fields)


#: The declared (snake_case) attribute set of each ``update_*`` target's crucible model —
#: the vocabulary of keys a ``fields`` map may carry. Derived from the models, so it can
#: never drift from what the projector's ``setattr`` would actually land.
_KNOWN_UPDATE_FIELDS: dict[str, frozenset[str]] = {
    "update_spec": _known(Specification),
    "update_epic": _known(Epic),
    "update_ticket": _known(Ticket),
    "update_blueprint": _known(Blueprint),
}


def unknown_field_key(op: PlanningOperationName, payload: Mapping[str, Any]) -> PrecheckResult:
    """Deny an ``update_*`` whose ``fields`` map carries a key not on the target model."""
    known = _KNOWN_UPDATE_FIELDS.get(op)
    if known is None:
        return Accepted()

    fields = payload.get("fields")
    # A missing / mistyped ``fields`` is schema_validate's job, not this gate's.
    if not isinstance(fields, Mapping):
        return Accepted()

    unknown = sorted(str(key) for key in fields if to_snake(str(key)) not in known)
    if not unknown:
        return Accepted()

    entity = op[len("update_") :]
    blockers = [
        f"fields.{key}: not a writable {entity} field — check for a typo or a mis-wrapped payload"
        for key in unknown
    ]
    return Denied(
        code="invalid_payload",
        message=(
            f"Payload for operation '{op}' nests {len(unknown)} unrecognized key(s) under "
            "'fields'. A key that is not a real field is dropped silently and the edit writes "
            "nothing — correct these and re-send:\n- " + "\n- ".join(blockers)
        ),
        context={"unknown_fields": unknown},
        blockers=blockers,
    )
