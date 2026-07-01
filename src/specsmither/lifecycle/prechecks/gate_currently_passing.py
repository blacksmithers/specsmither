"""Pre-check: is the phase gate currently passing? (the CPS gate)

Port of ``planning/pre-checks/gate-currently-passing.ts`` (A1 §1.4, §3.3) with
the cache TTL **removed** (locked decision 3: ALWAYS re-validate).

The TS source trusts ``session.lastGateResult == 'pass'`` while a 5-minute TTL
(``gateResultCacheTtlMs``) is unexpired, re-validating only when stale or
non-passing. Locally, validation is in-process and cheap, so the staleness knob
adds risk (a human edit can break a "fresh" gate) for no benefit: this port
**drops** ``Date.now()`` / ``gateResultCacheTtlMs`` entirely and always calls
``validator.validate(spec_full, session.current_phase, validator_config)``.
``session.last_gate_result`` / ``last_validated_at`` are never trusted here.

Because the lifecycle ``config`` argument existed solely to carry the now-removed
TTL, it is dropped from the signature (the L4 caller must NOT pass it).

A non-passing gate → :class:`Denied` (``gate_not_passed``) carrying the failing
findings' messages as ``blockers`` so the denial can surface the CURRENT blockers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from specsmither.db.models import PlanningSession
from specsmither.domain.enums import PlanningPhase
from specsmither.lifecycle.ports import SpecFull, Validator
from specsmither.lifecycle.prechecks.result import Accepted, Denied, PrecheckResult

__all__ = ["gate_currently_passing"]


def gate_currently_passing(
    session: PlanningSession,
    spec_full: SpecFull | None,
    validator: Validator,
    validator_config: Mapping[str, Any],
) -> PrecheckResult:
    """Re-validate the spec at the session's current phase → :class:`Accepted` | :class:`Denied`.

    ALWAYS calls the validator (no cache, no TTL). ``spec_full`` is ``None`` only
    if the spec could not be loaded, in which case the gate cannot be confirmed
    and the call is denied.
    """

    if spec_full is None:
        return Denied(
            code="gate_not_passed",
            message=(
                "Gate cannot be confirmed: the spec could not be loaded. "
                "Continue working on this phase before completing."
            ),
            context={"last_gate_result": session.last_gate_result},
            blockers=[],
        )

    phase = PlanningPhase(session.current_phase)
    output = validator.validate(spec_full, phase, validator_config)

    if output.gate_result != "pass":
        return Denied(
            code="gate_not_passed",
            message=(
                "Gate is not currently passing. Continue working on this phase "
                "before completing."
            ),
            context={"gate_result": output.gate_result, "phase": phase.value},
            blockers=[f.message for f in output.findings],
        )

    return Accepted()
