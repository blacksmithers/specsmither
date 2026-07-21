"""Config resolution — the pure merge layer over the :class:`ConfigStore` seam.

Two config domains
feed the lifecycle; both resolve the same way — baseline defaults, then the project
override, then (if a spec is in play) the frozen spec snapshot — but they merge with
different machinery:

* **``planning``** — the crucible ``ValidatorConfig`` (thresholds, tiers, topology,
  cross-validation). Owned by crucible; merged by :func:`crucible.merge_config`
  (which deep-merges *and re-validates* the result). :func:`resolve_validator_config`
  layers ``crucible.load_defaults()`` ← project overrides ← spec snapshot.

* **``planning-lifecycle``** — the lifecycle guidance knobs. After the config trim
  (and dropping ``observatoryBaseUrl`` — observatory links are cosmetic prose — and
  ``gateResultCacheTtlMs`` — the CPS stale-cache TTL is baked locally), this is just
  :data:`PLANNING_LIFECYCLE_DEFAULTS`: ``guidance.{maxNextEntitiesToShow,
  fieldRenderDetail}``. :func:`resolve_lifecycle_config` layers it via the pure
  :func:`deep_merge` (no schema, no validation — the shape is tiny and fixed).

Both resolvers read from a :class:`specsmither.lifecycle.ports.ConfigStore`. They are
plain sync functions — SpecSmither has no async I/O and assembles the effective
config straight from the store (``ports.py`` ``LifecyclePorts.config_store``).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import crucible
from crucible.i18n import normalize_language

if TYPE_CHECKING:
    from specsmither.lifecycle.ports import ConfigStore

__all__ = [
    "PLANNING_DOMAIN",
    "PLANNING_LIFECYCLE_CONFIG_SCHEMA_VERSION",
    "PLANNING_LIFECYCLE_DEFAULTS",
    "PLANNING_LIFECYCLE_DOMAIN",
    "deep_merge",
    "resolve_lifecycle_config",
    "resolve_validator_config",
]

#: The crucible ``ValidatorConfig`` namespace (mirrors ``crucible.PLANNING_CONFIG_DOMAIN``).
PLANNING_DOMAIN = "planning"
#: The lifecycle-guidance namespace.
PLANNING_LIFECYCLE_DOMAIN = "planning-lifecycle"
#: The schema version stamped on a ``planning-lifecycle`` write (1→2 at the
#: guidance-config trim; 2→3 adding ``guidance.language``). Surfaced for the
#: spec-create snapshot freeze (L4).
PLANNING_LIFECYCLE_CONFIG_SCHEMA_VERSION = 3

#: Baseline ``planning-lifecycle`` config — merged under project/spec overrides.
#: Trimmed to the surviving guidance knobs (see module docstring).
PLANNING_LIFECYCLE_DEFAULTS: dict[str, Any] = {
    "guidance": {
        # Max entities shown in the expansion-phase "next to fill" block. Caps size.
        "maxNextEntitiesToShow": 3,
        # Phase-entry field-block fidelity: 'rich' (full per-field render) | 'concise'.
        "fieldRenderDetail": "rich",
        # Guidance language for the composer's prose AND the crucible findings seam.
        # 'en' (default) | 'pt-br' (aliases 'pt'/'pt_BR'); normalized on read. An
        # unsupported tag falls back to 'en' rather than failing the lifecycle.
        "language": "en",
    },
}


def deep_merge(base: dict[str, Any], override: Any) -> dict[str, Any]:
    """Recursively merge *override* onto *base* (the PURE resolver merge).

    Rules (identical for both config domains):

    * a non-mapping *override* (``None``, a list, a primitive) leaves *base* unchanged;
    * a ``None`` value at any key is skipped (an override never *unsets* a default);
    * a list or primitive value **replaces** the corresponding key wholesale;
    * two mappings at the same key **merge recursively**.

    Never mutates either argument — *base* is shallow-copied per level (the resolvers
    feed a deep copy of the defaults, so nested defaults are never aliased into a
    returned result that a caller might mutate).
    """
    if not isinstance(override, dict):
        return dict(base)
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        existing = result.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            # Two mappings → merge recursively (lists/primitives fall through, replacing).
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def resolve_validator_config(
    config_store: ConfigStore, project_id: str, spec_id: str | None = None
) -> dict[str, Any]:
    """Resolve the effective crucible ``ValidatorConfig`` (domain ``planning``).

    Layers ``crucible.load_defaults()`` ← the project's overrides ← (if *spec_id* is
    given) the spec's frozen snapshot, via :func:`crucible.merge_config` (deep-merge +
    re-validate). With no stored overrides this returns the crucible defaults unchanged.
    """
    overrides: list[dict[str, Any]] = []
    project_overrides = config_store.get_project_overrides(project_id, PLANNING_DOMAIN)
    if project_overrides is not None:
        overrides.append(project_overrides)
    if spec_id is not None:
        snapshot = config_store.get_spec_snapshot(spec_id, PLANNING_DOMAIN)
        if snapshot is not None:
            overrides.append(snapshot)
    return crucible.merge_config(crucible.load_defaults(), *overrides)


def resolve_lifecycle_config(
    config_store: ConfigStore,
    project_id: str,
    spec_id: str | None = None,
    default_language: str = "en",
) -> dict[str, Any]:
    """Resolve the effective ``PlanningLifecycleConfig`` (domain ``planning-lifecycle``).

    Layers :data:`PLANNING_LIFECYCLE_DEFAULTS` ← the project's overrides ← (if
    *spec_id* is given) the spec's frozen snapshot, via the pure :func:`deep_merge`.
    With no stored overrides this returns the defaults. The defaults are deep-copied
    up front so the module-level constant is never aliased into the result.

    ``default_language`` seeds the baseline ``guidance.language`` (normalized; an
    unsupported tag degrades to ``"en"``) — the ambient default a caller supplies (the
    embed's ``LifecyclePorts.default_language`` / the product's ``SPECSMITHER_LANGUAGE``).
    A per-project or per-spec ``guidance.language`` override still wins over it.
    """
    base = copy.deepcopy(PLANNING_LIFECYCLE_DEFAULTS)
    base["guidance"]["language"] = normalize_language(default_language) or "en"
    result = deep_merge(
        base,
        config_store.get_project_overrides(project_id, PLANNING_LIFECYCLE_DOMAIN),
    )
    if spec_id is not None:
        result = deep_merge(
            result,
            config_store.get_spec_snapshot(spec_id, PLANNING_LIFECYCLE_DOMAIN),
        )
    return result
