"""Rich per-field guidance renderer — SpecForge's ``field_instruction`` catalog, ported.

0.1.0 shipped a deliberately-minimal English guidance composer (see
:mod:`specsmither.lifecycle.guidance.compose`). An LLM actor could not drive the planning
spec to a passing gate from that terse prose because it never saw the per-field
requirements: shape, required/optional, minimum count, N/A-eligibility (and the exact
``fieldDeclarations`` syntax used to declare it), tier, or worked examples. SpecForge's
simulator succeeded precisely because its guidance rendered a RICH per-field catalog. This
module ports that renderer so the same rich block feeds the SpecSmither actor.

Ported from (SpecForge lineage, **M8.6.1** — "fields-to-fill are delivered through the
PROSE, never a wire payload"):

* ``packages/lifecycle/src/planning/process-guidance/compose-field-instructions.ts`` —
  the ``renderField`` / ``naClause`` / ``composeFieldInstructions`` renderer and the
  ``field_instruction`` template body (``packages/lifecycle/catalogs/templates.yaml``).
* ``packages/lifecycle/src/planning/lifecycle-planning-guidance/compose-fields-to-fill.ts``
  — ``composeFieldsToFill`` + ``detectFieldState`` (missing / partial / complete from the
  current spec snapshot + ``minCount``), plus the ``buildFieldsBlock`` helper of
  ``compose-phase-intro.ts`` (which excludes ``autoPopulated`` system-set fields).

The per-field block is reproduced byte-for-byte with SpecForge's ``renderField`` output —
including the two-space template indent that lands the first interview-hook line at eight
columns and the blank lines a field with no ``minCount``/``tier`` leaves before *Examples*.
The phase header is the SpecSmither single-newline variant of the ``phase_intro`` template.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["compose_field_instructions"]

#: Import-package that owns the ``catalogs/`` data directory.
_CATALOG_PACKAGE = "specsmither.lifecycle.guidance"

_FieldState = Literal["empty", "partial", "filled"]


@dataclass(frozen=True)
class _FieldEntry:
    """One catalog field (the ``FieldEntry`` schema of ``lifecycle-planning-guidance``)."""

    field: str
    required: bool
    shape: str
    description: str
    interview_hooks: tuple[str, ...]
    na_eligible: bool
    na_when: str | None
    min_count: int | None
    tier: str | None
    examples: tuple[str, ...]
    auto_populated: bool


@dataclass(frozen=True)
class _PhaseCatalog:
    """A parsed phase catalog (``PhaseData``, trimmed to the fields the renderer reads)."""

    phase_human_name: str
    phase_index: int
    phase_goal: str
    phase_character: str
    fields: tuple[_FieldEntry, ...]


# --------------------------------------------------------------------------- #
# Catalog loading (importlib.resources + per-phase cache)                      #
# --------------------------------------------------------------------------- #

#: Per-phase catalog cache. ``None`` memoizes "no catalog for this phase" (e.g. ``planned``).
_CATALOG_CACHE: dict[str, _PhaseCatalog | None] = {}


def _load_catalog(phase: str) -> _PhaseCatalog | None:
    """Load (and cache) the ``catalogs/<phase>.yaml`` catalog; ``None`` when absent."""

    if phase in _CATALOG_CACHE:
        return _CATALOG_CACHE[phase]
    catalog = _read_catalog(phase)
    _CATALOG_CACHE[phase] = catalog
    return catalog


def _read_catalog(phase: str) -> _PhaseCatalog | None:
    """Read + parse the packaged YAML catalog for ``phase`` (``None`` when there is none)."""

    resource = files(_CATALOG_PACKAGE).joinpath(f"catalogs/{phase}.yaml")
    if not resource.is_file():
        return None
    raw: Any = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    fields = tuple(
        _parse_field(entry) for entry in raw.get("fields", []) if isinstance(entry, dict)
    )
    return _PhaseCatalog(
        phase_human_name=str(raw.get("phaseHumanName", "")),
        phase_index=int(raw.get("phaseIndex", 0)),
        phase_goal=str(raw.get("phaseGoal", "")),
        phase_character=str(raw.get("phaseCharacter", "")),
        fields=fields,
    )


def _parse_field(raw: Mapping[str, Any]) -> _FieldEntry:
    """Coerce one raw catalog entry into a typed :class:`_FieldEntry`.

    Mirrors ``composeFieldsToFill``: ``description`` falls back to the field name when the
    catalog omits it; ``shape`` defaults to ``string``; missing lists become empty tuples.
    """

    field_name = str(raw.get("field", ""))
    description = raw.get("description")
    shape = raw.get("shape")
    na_when = raw.get("na_when")
    tier = raw.get("tier")
    min_count = raw.get("minCount")
    hooks = raw.get("interview_hooks") or []
    examples = raw.get("examples") or []
    return _FieldEntry(
        field=field_name,
        required=bool(raw.get("required", False)),
        shape=str(shape) if shape else "string",
        description=str(description) if description is not None else field_name,
        interview_hooks=tuple(str(hook) for hook in hooks),
        na_eligible=bool(raw.get("naEligible", False)),
        na_when=str(na_when) if na_when else None,
        min_count=int(min_count) if isinstance(min_count, int) else None,
        tier=str(tier) if tier else None,
        examples=tuple(str(example) for example in examples),
        auto_populated=bool(raw.get("autoPopulated", False)),
    )


# --------------------------------------------------------------------------- #
# Field-state detection (compose-fields-to-fill.ts detectFieldState)           #
# --------------------------------------------------------------------------- #


def _field_state(field: _FieldEntry, snapshot: Mapping[str, Any]) -> _FieldState:
    """State of ``field`` against the flat ``snapshot`` (the ``composeFieldsToFill`` lookup).

    The snapshot is probed by the dot-stripped key first (``epic.scope.inScope`` →
    ``scope.inScope``) then by the full field id, mirroring the TS ``?.[fieldKey] ??
    ?.[entry.field]`` coalescing (only a *missing* first key falls through).
    """

    stripped = field.field.split(".", 1)[1] if "." in field.field else field.field
    value = snapshot.get(stripped)
    if value is None:
        value = snapshot.get(field.field)
    return _detect_field_state(value, field)


def _detect_field_state(value: Any, field: _FieldEntry) -> _FieldState:
    """``empty`` (absent/blank) / ``partial`` (list shorter than ``minCount``) / ``filled``."""

    if value is None or value == "":
        return "empty"
    if isinstance(value, list):
        if len(value) == 0:
            return "empty"
        if field.min_count is not None and len(value) < field.min_count:
            return "partial"
        return "filled"
    if isinstance(value, dict) and len(value) == 0:
        return "empty"
    return "filled"


# --------------------------------------------------------------------------- #
# Per-field rendering (compose-field-instructions.ts renderField / naClause)   #
# --------------------------------------------------------------------------- #


def _bullet(items: Sequence[str], indent: str = "      ") -> str:
    """Bulleted list (``bullet`` helper). ``(none)`` when empty; each item at ``indent``."""

    if items:
        return "\n".join(f"{indent}- {item}" for item in items)
    return f"{indent}- (none)"


def _na_clause(field: _FieldEntry) -> str:
    """The field-level N/A instruction (``naClause`` — the real M8.6.3 mechanism)."""

    if not field.na_eligible:
        return "Not N/A-eligible — must be filled."
    when = f" when {field.na_when}" if field.na_when else ""
    return (
        f"N/A-eligible{when}: declare via `update_*` with "
        f'`fieldDeclarations: {{ "{field.field}": '
        f'{{ "value": "N/A", "reason": "<≥20 chars>" }} }}`.'
    )


def _render_field(field: _FieldEntry) -> str:
    """Render one field via the ``field_instruction`` template body (byte-exact)."""

    required_label = "(required)" if field.required else "(optional)"
    min_count_clause = (
        f"    - Minimum count: {field.min_count}" if field.min_count is not None else ""
    )
    tier_clause = f"    - Tier: {field.tier}" if field.tier else ""
    return (
        f"For **`{field.field}`** {required_label}:\n"
        f"- Shape: `{field.shape}`\n"
        f"- {field.description}\n"
        f"- Interview hooks:\n"
        f"  {_bullet(field.interview_hooks)}\n"
        f"- {_na_clause(field)}\n"
        f"{min_count_clause}\n"
        f"{tier_clause}\n"
        f"\n"
        f"Examples:\n"
        f"{_bullet(field.examples)}\n"
    )


def _compose_field_instructions(fields: Sequence[_FieldEntry]) -> str:
    """The full per-field block for a phase's fields (``composeFieldInstructions``)."""

    if not fields:
        return "(no fillable fields in this phase)"
    return "\n".join(_render_field(field) for field in fields)


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #


def compose_field_instructions(
    phase: str,
    spec_snapshot: Mapping[str, Any] | None = None,
) -> str:
    """Render the rich per-field guidance block for ``phase``.

    Returns ``""`` for phases with no catalog (e.g. ``planned``). System-set /
    not-agent-fillable fields (``autoPopulated``) are always excluded — the actor never
    fills those. When ``spec_snapshot`` is given only the fields still needing work
    (``empty`` / ``partial``) render; when it is ``None`` the full phase catalog renders
    (the phase-intro / orientation view). The block is prefixed with the phase header.
    """

    catalog = _load_catalog(phase)
    if catalog is None:
        return ""

    # buildFieldsBlock (M8.6.6): exclude system-set fields the agent never fills.
    fields = [field for field in catalog.fields if not field.auto_populated]
    if spec_snapshot is not None:
        fields = [
            field
            for field in fields
            if _field_state(field, spec_snapshot) in ("empty", "partial")
        ]

    header = (
        f"You are in phase {catalog.phase_index} of 6: {catalog.phase_human_name}.\n"
        f"Goal: {catalog.phase_goal}\n"
        f"Character: {catalog.phase_character}\n"
        f"\n"
        f"What to do in this phase:"
    )
    return f"{header}\n{_compose_field_instructions(fields)}".rstrip()
