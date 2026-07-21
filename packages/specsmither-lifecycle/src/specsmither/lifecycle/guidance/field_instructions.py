"""Rich per-field guidance renderer — the ``field_instruction`` catalog.

0.1.0 shipped a deliberately-minimal English guidance composer (see
:mod:`specsmither.lifecycle.guidance.compose`). An LLM actor could not drive the planning
spec to a passing gate from that terse prose because it never saw the per-field
requirements: shape, required/optional, minimum count, N/A-eligibility (and the exact
``fieldDeclarations`` syntax used to declare it), tier, or worked examples. A rich
per-field catalog is what lets an actor drive the spec to a passing gate. This
module renders that rich block for the SpecSmither actor.

The renderer has two parts, both delivering fields-to-fill through the PROSE,
never a wire payload:

* ``renderField`` / ``naClause`` / ``composeFieldInstructions`` — the per-field
  renderer and the ``field_instruction`` template body.
* ``composeFieldsToFill`` + ``detectFieldState`` (missing / partial / complete from
  the current spec snapshot + ``minCount``), plus the ``buildFieldsBlock`` helper
  (which excludes ``autoPopulated`` system-set fields).

The per-field block's exact layout matters — including the two-space template indent
that lands the first interview-hook line at eight columns and the blank lines a field
with no ``minCount``/``tier`` leaves before *Examples*. The phase header is the
SpecSmither single-newline variant of the ``phase_intro`` template.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

import yaml

from specsmither.lifecycle.i18n import t, text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = ["compose_field_instructions"]

#: Import-package that owns the ``catalogs/`` data directory.
_CATALOG_PACKAGE = "specsmither.lifecycle.guidance"

#: The canonical guidance language; a per-phase catalog with no ``<phase>.<lang>.yaml``
#: overlay renders in this language.
_DEFAULT_LANGUAGE = "en"

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

#: Per-(phase, language) catalog cache. ``None`` memoizes "no catalog for this phase"
#: (e.g. ``planned``).
_CATALOG_CACHE: dict[tuple[str, str], _PhaseCatalog | None] = {}


def _load_catalog(phase: str, language: str = _DEFAULT_LANGUAGE) -> _PhaseCatalog | None:
    """Load (and cache) the ``catalogs/<phase>.yaml`` catalog for ``language``.

    The English catalog is the source of truth; a non-default ``language`` overlays
    the translated phase-content prose from ``catalogs/<phase>.<language>.yaml`` (a
    per-key/per-field fallback to English), so a missing overlay renders in English.
    ``None`` when the phase has no catalog at all.
    """

    key = (phase, language)
    if key in _CATALOG_CACHE:
        return _CATALOG_CACHE[key]
    catalog = _read_catalog(phase)
    if catalog is not None and language != _DEFAULT_LANGUAGE:
        catalog = _overlay_catalog(catalog, phase, language)
    _CATALOG_CACHE[key] = catalog
    return catalog


def _overlay_catalog(base: _PhaseCatalog, phase: str, language: str) -> _PhaseCatalog:
    """Overlay ``base`` with the translated prose from ``<phase>.<language>.yaml``.

    Only natural-language prose is overlaid — phase goal/character/human-name and,
    per field (matched by ``field`` id), ``description`` / ``interview_hooks`` /
    ``na_when``. Structural facts (required, shape, minCount, tier, naEligible,
    autoPopulated) and code-literal ``examples`` stay canonical. A missing overlay
    file, key, or field falls back to the English value.
    """

    resource = files(_CATALOG_PACKAGE).joinpath(f"catalogs/{phase}.{language}.yaml")
    if not resource.is_file():
        return base
    raw: Any = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return base
    by_id: dict[str, Mapping[str, Any]] = {
        str(entry.get("field", "")): entry
        for entry in raw.get("fields", [])
        if isinstance(entry, dict)
    }
    fields = tuple(_overlay_field(field, by_id.get(field.field)) for field in base.fields)
    return replace(
        base,
        phase_human_name=str(raw.get("phaseHumanName") or base.phase_human_name),
        phase_goal=str(raw.get("phaseGoal") or base.phase_goal),
        phase_character=str(raw.get("phaseCharacter") or base.phase_character),
        fields=fields,
    )


def _overlay_field(field: _FieldEntry, tr: Mapping[str, Any] | None) -> _FieldEntry:
    """Overlay one field's translatable prose (``None`` → the English entry unchanged)."""

    if tr is None:
        return field
    description = tr.get("description")
    na_when = tr.get("na_when")
    hooks = tr.get("interview_hooks")
    return replace(
        field,
        description=str(description) if description is not None else field.description,
        na_when=str(na_when) if na_when else field.na_when,
        interview_hooks=(
            tuple(str(hook) for hook in hooks)
            if isinstance(hooks, list) and hooks
            else field.interview_hooks
        ),
    )


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
# Field-state detection (detectFieldState)                                     #
# --------------------------------------------------------------------------- #


def _field_state(field: _FieldEntry, snapshot: Mapping[str, Any]) -> _FieldState:
    """State of ``field`` against the flat ``snapshot`` (the ``composeFieldsToFill`` lookup).

    The snapshot is probed by the dot-stripped key first (``epic.scope.inScope`` →
    ``scope.inScope``) then by the full field id — a coalescing lookup where only a
    *missing* first key falls through.
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
# Per-field rendering (renderField / naClause)                                 #
# --------------------------------------------------------------------------- #


def _bullet(
    items: Sequence[str], indent: str = "      ", language: str = _DEFAULT_LANGUAGE
) -> str:
    """Bulleted list (``bullet`` helper). ``(none)`` when empty; each item at ``indent``."""

    if items:
        return "\n".join(f"{indent}- {item}" for item in items)
    return f"{indent}- {text(language, 'field.bulletNone')}"


def _na_clause(field: _FieldEntry, language: str = _DEFAULT_LANGUAGE) -> str:
    """The field-level N/A instruction (``naClause``).

    Post-justify, N/A is declared with the dedicated ``justify`` op (a late
    ``update_*`` carrying only ``fieldDeclarations`` is stripped, then rolls back),
    so the clause names the ``justify`` op + the bare N/A scope (the field id with its
    ``spec.`` / ``epic.`` / ``ticket.`` prefix dropped — the scope ``justify`` validates).
    """

    if not field.na_eligible:
        return text(language, "field.naNotEligible")
    when = t(language, "field.naWhen", {"naWhen": field.na_when}) if field.na_when else ""
    scope = field.field.split(".", 1)[1] if "." in field.field else field.field
    return t(language, "field.naEligible", {"when": when, "scope": scope})


def _render_field(field: _FieldEntry, language: str = _DEFAULT_LANGUAGE) -> str:
    """Render one field via the ``field_instruction`` template body."""

    required_key = "field.required" if field.required else "field.optional"
    min_count_clause = (
        t(language, "field.minCount", {"minCount": field.min_count})
        if field.min_count is not None
        else ""
    )
    tier_clause = t(language, "field.tier", {"tier": field.tier}) if field.tier else ""
    return t(
        language,
        "field.render",
        {
            "field": field.field,
            "requiredLabel": text(language, required_key),
            "shape": field.shape,
            "description": field.description,
            "hooks": _bullet(field.interview_hooks, language=language),
            "naClause": _na_clause(field, language),
            "minCountClause": min_count_clause,
            "tierClause": tier_clause,
            "examples": _bullet(field.examples, language=language),
        },
    )


def _compose_field_instructions(
    fields: Sequence[_FieldEntry], language: str = _DEFAULT_LANGUAGE
) -> str:
    """The full per-field block for a phase's fields (``composeFieldInstructions``)."""

    if not fields:
        return text(language, "field.noneFillable")
    return "\n".join(_render_field(field, language) for field in fields)


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #


def compose_field_instructions(
    phase: str,
    spec_snapshot: Mapping[str, Any] | None = None,
    language: str = _DEFAULT_LANGUAGE,
) -> str:
    """Render the rich per-field guidance block for ``phase`` in ``language``.

    Returns ``""`` for phases with no catalog (e.g. ``planned``). System-set /
    not-agent-fillable fields (``autoPopulated``) are always excluded — the actor never
    fills those. When ``spec_snapshot`` is given only the fields still needing work
    (``empty`` / ``partial``) render; when it is ``None`` the full phase catalog renders
    (the phase-intro / orientation view). The block is prefixed with the phase header.
    A non-default ``language`` overlays the translated phase-content prose (English
    fallback per key); the structural frame comes from the shared text catalog.
    """

    catalog = _load_catalog(phase, language)
    if catalog is None:
        return ""

    # buildFieldsBlock: exclude system-set fields the agent never fills.
    fields = [field for field in catalog.fields if not field.auto_populated]
    if spec_snapshot is not None:
        fields = [
            field
            for field in fields
            if _field_state(field, spec_snapshot) in ("empty", "partial")
        ]

    header = t(
        language,
        "field.header",
        {
            "idx": catalog.phase_index,
            "name": catalog.phase_human_name,
            "goal": catalog.phase_goal,
            "character": catalog.phase_character,
        },
    )
    return f"{header}\n{_compose_field_instructions(fields, language)}".rstrip()
