"""Guidance internationalization for the lifecycle's own prose.

The lifecycle's canonical guidance language is English. A project opts into a
translation via the ``planning-lifecycle`` guidance config
(``guidance.language``, e.g. ``"pt-br"``); the resolved tag threads through
:func:`compose_response` into every body the composer renders, and into the
validator seam so crucible emits matching-language findings.

This module owns the LIFECYCLE catalog (:data:`TEXT_EN` + the ``data/<lang>.json``
overlays); the language-tag semantics — the supported set, the alias table, and
the ``{placeholder}`` substitution — are shared with crucible so the two layers
never drift (a project that asks for ``pt-br`` gets it from both). Translations
OVERLAY the English defaults: a missing key falls back to English, so a partial
catalog degrades gracefully and the default ``"en"`` output is byte-identical to
the pre-i18n composer.

Catalog shape (``data/<language>.json``)::

    { "text": { "<catalog key>": "<translated template>", ... } }

Templates use ``{placeholder}`` markers substituted by :func:`render` via literal
replacement — braces that are part of the prose (an operation-call snippet, a
``fieldDeclarations`` example) pass through untouched.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Any, cast

from crucible.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    normalize_language,
    render,
)

from .catalog_en import TEXT_EN

__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "TEXT_EN",
    "normalize_language",
    "render",
    "resolve_language",
    "t",
    "text",
]


@cache
def _catalog(language: str) -> dict[str, dict[str, str]]:
    if language == DEFAULT_LANGUAGE:
        return {}
    raw = (
        resources.files("specsmither.lifecycle.i18n.data")
        .joinpath(f"{language}.json")
        .read_text(encoding="utf-8")
    )
    return cast("dict[str, dict[str, str]]", json.loads(raw))


def text(language: str, key: str) -> str:
    """Resolve a catalog string by key, falling back to the canonical English.

    Raises :class:`KeyError` for a key absent from the English catalog (a typo,
    not a missing translation).
    """
    override = _catalog(language).get("text", {}).get(key)
    return override if override is not None else TEXT_EN[key]


def resolve_language(lifecycle_config: Mapping[str, Any] | None) -> str:
    """The normalized guidance language from the resolved lifecycle config.

    Reads ``guidance.language`` (per-project, frozen into the spec snapshot),
    canonicalizes it (``pt_BR``/``pt`` → ``pt-br``), and falls back to
    :data:`DEFAULT_LANGUAGE` for an absent, non-string, or unsupported value — so
    a caller always threads a tag both this composer and crucible accept.
    """
    if lifecycle_config is None:
        return DEFAULT_LANGUAGE
    guidance = lifecycle_config.get("guidance")
    raw = guidance.get("language") if isinstance(guidance, dict) else None
    if not isinstance(raw, str):
        return DEFAULT_LANGUAGE
    return normalize_language(raw) or DEFAULT_LANGUAGE


def t(language: str, key: str, subs: Mapping[str, Any] | None = None) -> str:
    """Resolve ``key`` for ``language`` and substitute ``{placeholder}`` markers.

    The one-call convenience over :func:`text` + :func:`render` the composer uses.
    """
    template = text(language, key)
    return render(template, subs) if subs else template
