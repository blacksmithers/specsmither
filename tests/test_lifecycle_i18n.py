"""Guidance internationalization for the lifecycle's own prose.

Covers the i18n primitives (:func:`resolve_language`, :func:`text` overlay +
English fallback) and the end-to-end effect on the composer: with
``guidance.language = "pt-br"`` the variant bodies and the per-field scaffolding
render in Brazilian Portuguese, while the default (``"en"`` / absent config) stays
byte-for-byte English.
"""

from __future__ import annotations

from typing import Any

import pytest

from specsmither.db.models import PlanningSession
from specsmither.domain.enums import GuidanceVariant, PlanningPhase, PlanningSessionStatus
from specsmither.lifecycle.guidance.compose import compose_response
from specsmither.lifecycle.guidance.field_instructions import compose_field_instructions
from specsmither.lifecycle.i18n import resolve_language, text

VALIDATOR_CONFIG: dict[str, Any] = {
    "thresholds": {"specification": 80, "epic": 75, "ticket": 70},
}
PT_BR: dict[str, Any] = {"guidance": {"language": "pt-br"}}


def _session(**overrides: Any) -> PlanningSession:
    defaults: dict[str, Any] = {
        "id": "ps-1",
        "specification_id": "spec-1",
        "status": PlanningSessionStatus.ACTIVE.value,
        "current_phase": PlanningPhase.EPIC_EXPANSION.value,
        "actions_count": 4,
        "last_score": 92.0,
        "last_gate_result": "pass",
    }
    defaults.update(overrides)
    return PlanningSession(**defaults)


# --------------------------------------------------------------------------- #
# primitives                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"guidance": {"language": "pt-br"}}, "pt-br"),
        ({"guidance": {"language": "pt"}}, "pt-br"),
        ({"guidance": {"language": "pt_BR"}}, "pt-br"),
        ({"guidance": {"language": "PT-BR"}}, "pt-br"),
        ({"guidance": {"language": "en"}}, "en"),
        ({"guidance": {"language": "klingon"}}, "en"),  # unsupported → fallback
        ({"guidance": {}}, "en"),
        ({}, "en"),
        (None, "en"),
    ],
)
def test_resolve_language_normalizes_with_english_fallback(
    raw: dict[str, Any] | None, expected: str
) -> None:
    assert resolve_language(raw) == expected


def test_text_overlays_pt_br_and_falls_back_to_english() -> None:
    # A translated key returns the pt-br override.
    assert text("pt-br", "label.notScored") == "ainda não pontuado"
    # English is unchanged.
    assert text("en", "label.notScored") == "not yet scored"


def test_text_raises_on_unknown_key() -> None:
    with pytest.raises(KeyError):
        text("en", "no.such.key")
    # Even in a translated language, an unknown key is a typo, not a missing translation.
    with pytest.raises(KeyError):
        text("pt-br", "no.such.key")


# --------------------------------------------------------------------------- #
# composer end-to-end                                                          #
# --------------------------------------------------------------------------- #


def test_gate_passed_body_is_english_by_default() -> None:
    response = compose_response(
        variant=GuidanceVariant.GATE_PASSED,
        session=_session(),
        validator_config=VALIDATOR_CONFIG,
    )
    assert response.guidance.startswith("Phase 3 of 6 — Epic Expansion")
    assert "the gate is passing" in response.guidance


def test_gate_passed_body_is_pt_br_when_configured() -> None:
    response = compose_response(
        variant=GuidanceVariant.GATE_PASSED,
        session=_session(),
        lifecycle_config=PT_BR,
        validator_config=VALIDATOR_CONFIG,
    )
    assert response.guidance.startswith("Fase 3 de 6 — Expansão de Épicos")
    assert "o gate está passando" in response.guidance
    assert "`complete_planning_session`" in response.guidance  # op names stay canonical


def test_denied_body_is_pt_br_frame_over_canonical_message() -> None:
    from specsmither.lifecycle.prechecks import Denied

    response = compose_response(
        variant=GuidanceVariant.DENIED,
        session=_session(),
        denial=Denied(code="x", message="Some reason.", blockers=["a", "b"]),
        lifecycle_config=PT_BR,
        validator_config=VALIDATOR_CONFIG,
    )
    assert response.guidance == "Negado: Some reason. Motivos do bloqueio: a; b."


def test_field_instructions_scaffolding_is_pt_br() -> None:
    en = compose_field_instructions("planning_spec")
    pt = compose_field_instructions("planning_spec", language="pt-br")
    assert "(required)" in en and "Interview hooks:" in en
    # Structural scaffolding is translated even before the phase-content overlay lands.
    assert "(obrigatório)" in pt
    assert "Ganchos de entrevista:" in pt
    assert "Você está na fase 1 de 6:" in pt
