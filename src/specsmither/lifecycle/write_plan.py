"""WritePlan builders + decompose-on-write (work item #8) — ported from
``planning/atomic-write/*`` (A1 §2.3) and the ``buildOpWriteItems`` /
``buildCreatePersist`` decompose in ``verbs/action-planning-session.ts`` (A1 §1.4
step 11).

These are the **pure** builders an L4 verb calls to turn a decision into the flat
:class:`~specsmither.adapters.write_plan_executor.WritePlan` the M0 executor commits
in one transaction. Each builder takes the already-built audit row dicts (from
:func:`~specsmither.lifecycle.audit.build_action` /
:func:`~specsmither.lifecycle.audit.build_transition`) and wraps them in the
matching :class:`~specsmither.adapters.write_plan_executor.WritePlanItem` variants.

What is DROPPED versus the TS originals (all DynamoDB transport artifacts):

* the ``transactions[][]`` chunker — the executor applies the FLAT ``items`` list in
  one ``BEGIN IMMEDIATE`` (no 100-item ``transactWrite`` cap), so there is no
  ``splitIntoTransactions``.
* the ``changes: ModelChange[]`` AppSync-subscription mirror — no subscribers locally.

The **CRITICAL SpecSmither divergence** (matches the M0 schema): there is no
per-entity score *column* on a spec/epic/ticket row, so the TS entity-score *fusion*
(writing a ``{spec|epic|ticket}Score`` field onto the mutation row) is dropped. The
gate's :class:`~specsmither.lifecycle.gate.EntityScoreWrite` rows become
:class:`~specsmither.adapters.write_plan_executor.ScoreDatapointAppend` items ONLY
(the ``planning_entity_score_datapoints`` time-series, id
``f"{trigger_action_id}#{entity_id}#{trigger}"``). ``EntityScoreUpdate`` is a no-op
in M0 and is not emitted.

The other deliberate fix (A1 §3.3 cache-divergence): the APS success write persists
``last_validator_output`` + ``last_score`` + ``last_gate_result`` +
``last_validated_at`` on the ``SessionUpdate`` so the session is the single source of
truth (the TS persisted only ``lastGateResult`` + ``lastValidatedAt``).

The decompose-on-write (``buildOpWriteItems`` / ``buildCreatePersist``) is ported
into :func:`build_create_persist` / :func:`build_op_write_items` /
:func:`extract_mutation_target`, combined by :func:`resolve_aps_mutation` into the
:class:`ApsMutation` that :func:`build_aps_success_write_plan` consumes. CREATE ops
mint an id + the full create columns into a ``SpecMutation``; ``update_ticket``
decomposes its child-backed arrays (acceptance criteria, implementation steps,
code/type snippets, blueprint refs, ``filesToBe*`` → ``ticket_file_changes``,
``testSpecification`` → ``ticket_tests``) into ``RelatedReplace`` items with
deterministic child ids and strips them off the flat ``SpecMutation``; deletes →
``EntityDelete``; dependencies → ``RelatedPut`` / ``RelatedDelete`` of
``ticket_dependencies`` rows (deterministic edge id ``f"{from}--requires--{to}"``).

The op payload keys are the agent/MCP **camelCase** wire shape (``epicId`` /
``fromTicketId`` / ``dependencyIds`` / ``acceptanceCriteria`` / …), matching the TS;
the *emitted* child-row keys are snake_case ORM columns (the executor would normalise
either way, but we prefer snake_case).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from specsmither.adapters.write_plan_executor import (
    ActionAppend,
    EntityDelete,
    EntityType,
    RecordTransition,
    RelatedDelete,
    RelatedPut,
    RelatedReplace,
    ScoreDatapointAppend,
    SessionCreate,
    SessionUpdate,
    SpecMutation,
    WritePlan,
    WritePlanItem,
)
from specsmither.db.base import new_ulid, now_iso
from specsmither.domain.enums import (
    ActorType,
    EpicStatus,
    PlanningPhase,
    PlanningSessionStatus,
    SpecStatus,
    TicketStatus,
    TransitionTrigger,
)

if TYPE_CHECKING:
    from specsmither.lifecycle.gate import EntityScoreWrite
    from specsmither.lifecycle.ports import IdGenerator, SpecFull, ValidatorOutput

__all__ = [
    "ApsMutation",
    "ApsRollback",
    "CreatePersist",
    "OpWriteItems",
    "build_approve_handover_write_plan",
    "build_aps_denied_write_plan",
    "build_aps_success_write_plan",
    "build_cps_denied_write_plan",
    "build_cps_success_write_plan",
    "build_create_persist",
    "build_gps_feedback_consume_write_plan",
    "build_gps_read_only_write_plan",
    "build_op_write_items",
    "build_reject_handover_with_feedback_write_plan",
    "build_reject_handover_write_plan",
    "build_sps_create_write_plan",
    "build_sps_deny_awaiting_write_plan",
    "build_sps_resume_write_plan",
    "ensure_item_ids",
    "extract_mutation_target",
    "resolve_aps_mutation",
]


# --------------------------------------------------------------------------- #
# small helpers                                                               #
# --------------------------------------------------------------------------- #


def _v(value: Any) -> Any:
    """Coerce an :class:`enum.Enum` member to its plain ``.value`` (passthrough else)."""
    return value.value if isinstance(value, Enum) else value


def _iso(now: datetime | str) -> str:
    """Normalise a ``now`` argument (``datetime`` or already-ISO string) to ISO-8601."""
    return now if isinstance(now, str) else now.isoformat()


def _mint(id_generator: IdGenerator | None) -> str:
    return (id_generator or new_ulid)()


def _now(clock: Any | None) -> str:
    return now_iso() if clock is None else _iso(clock())


def _guidance_fields(
    process_guidance: Mapping[str, Any] | None,
    lifecycle_planning_guidance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the latest-only guidance snapshot fields persisted on a ``SessionUpdate``."""
    fields: dict[str, Any] = {}
    if process_guidance is not None:
        fields["last_process_guidance"] = dict(process_guidance)
    if lifecycle_planning_guidance is not None:
        fields["last_lifecycle_planning_guidance"] = dict(lifecycle_planning_guidance)
    return fields


def _serialize_validator_output(output: ValidatorOutput) -> dict[str, Any]:
    """Render a :class:`ValidatorOutput` as a JSON-safe dict for the session cache blob.

    The blob carries ``validated_phase`` inside it (the SPS-resume cache is keyed on
    it); enum members are coerced to their plain ``.value`` strings so the stored JSON
    round-trips cleanly.
    """
    return {
        "gate_result": output.gate_result,
        "local_score": output.local_score,
        "per_epic_score": dict(output.per_epic_score),
        "per_ticket_score": dict(output.per_ticket_score),
        "findings": [
            {
                "category": _v(f.category),
                "message": f.message,
                "severity": f.severity,
                "entity_id": f.entity_id,
                "entity_type": f.entity_type,
                "path": f.path,
                "points_lost": f.points_lost,
                "global_impact_on_fix": f.global_impact_on_fix,
            }
            for f in output.findings
        ],
        "validated_phase": _v(output.validated_phase),
    }


def _score_datapoints(
    entity_score_writes: Sequence[EntityScoreWrite], trigger_action_id: str
) -> list[WritePlanItem]:
    """Map the gate's entity-score writes to ``ScoreDatapointAppend`` items ONLY.

    No fusion into the mutation row and no ``EntityScoreUpdate`` (there is no
    per-entity score column in the M0 schema). The executor derives the deterministic
    datapoint id ``f"{trigger_action_id}#{entity_id}#{trigger}"``.
    """
    return [
        ScoreDatapointAppend(
            entity_type=w.entity_type,
            entity_id=w.entity_id,
            score=w.score,
            trigger=w.trigger.value,
            trigger_action_id=trigger_action_id,
        )
        for w in entity_score_writes
    ]


# --------------------------------------------------------------------------- #
# decompose-on-write data contracts                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CreatePersist:
    """A minted CREATE target: a fresh entity id + the full create column set."""

    entity_type: Literal["epic", "ticket", "blueprint"]
    entity_id: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class OpWriteItems:
    """Op-specific write items beyond the single ``SpecMutation`` (``buildOpWriteItems``).

    * ``delete_items`` — REPLACE the ``SpecMutation`` with hard-delete items (delete ops).
    * ``extra_items`` — APPEND after the core items (``RelatedPut`` / ``RelatedDelete``
      for dependencies; ``RelatedReplace`` for the ``update_ticket`` child arrays).
    * ``fields_override`` — the residual flat ticket columns after a ``update_ticket``
      decompose (``None`` when the op was not decomposed → use the raw payload fields).
    """

    delete_items: list[WritePlanItem] = field(default_factory=list)
    extra_items: list[WritePlanItem] = field(default_factory=list)
    fields_override: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApsMutation:
    """The resolved primary mutation for an APS op (target + fields + side items)."""

    entity_type: EntityType
    entity_id: str
    fields: dict[str, Any]
    delete_items: list[WritePlanItem] = field(default_factory=list)
    extra_items: list[WritePlanItem] = field(default_factory=list)


@dataclass(frozen=True)
class ApsRollback:
    """A late-op rollback: the native (effective) phase + its ``RecordTransition`` row."""

    effective_phase: PlanningPhase | str
    transition: dict[str, Any]


# --------------------------------------------------------------------------- #
# decompose-on-write (buildCreatePersist / buildOpWriteItems)                  #
# --------------------------------------------------------------------------- #


#: MB.1.1 — spec/epic array-of-object content fields whose crucible sub-models require a
#: schema-internal ``id`` (and, for acceptance criteria, a positional ``order``). An agent
#: may author these items WITHOUT the id/order (the wire schema does not force them); the
#: read boundary (crucible sub-models) then silently drops the whole array to ``None`` — so
#: the authored content vanishes and the gate can never score it, stalling the drive. The
#: write-boundary filler mints the ids/orders BEFORE the write so every array round-trips.
#: Names are the camelCase wire keys (``update_spec`` / ``update_epic`` ``fields``).
_ID_ONLY_ARRAY_FIELDS = (
    "goals",
    "nonFunctionalRequirements",
    "guardrails",
    "techStack",
    "folderStructures",
    "fileStructures",
    "apiContracts",
    "sharedPatterns",
)
_ID_ORDER_ARRAY_FIELDS = ("acceptanceCriteria",)


def _fill_item_ids(
    items: Sequence[Any], id_generator: IdGenerator | None, *, order: bool
) -> list[Any]:
    """Mint a missing ``id`` (+ positional ``order`` when ``order``) into each mapping item."""
    out: list[Any] = []
    for i, item in enumerate(items):
        if isinstance(item, Mapping):
            d = dict(item)
            if not d.get("id"):
                d["id"] = _mint(id_generator)
            if order and d.get("order") is None:
                d["order"] = i + 1
            out.append(d)
        else:
            out.append(item)
    return out


def ensure_item_ids(
    fields: Mapping[str, Any], *, id_generator: IdGenerator | None = None
) -> dict[str, Any]:
    """MB.1.1 write-boundary filler: mint schema-required id/order into spec/epic arrays.

    Returns a shallow copy of ``fields`` with an ``id`` minted into every id-bearing
    array-of-object content field (and an ``order`` into acceptance criteria), including
    the nested ``requirements[].acceptanceCriteria[]``. Non-array / string-array fields
    (``requirementsCovered``, ``validationCommands``, ``tags`` …) are left untouched.
    """
    out = dict(fields)
    for name in _ID_ONLY_ARRAY_FIELDS:
        if isinstance(out.get(name), list):
            out[name] = _fill_item_ids(out[name], id_generator, order=False)
    # MB.1.2 — normalise apiContracts[].type case at the write boundary (REST -> rest) so a
    # correctly-cased-but-uppercase value round-trips through the crucible enum on read; a
    # value still off-enum after folding is soft-denied upstream (field_shape_soft_deny).
    contracts = out.get("apiContracts")
    if isinstance(contracts, list):
        normalised: list[Any] = []
        for contract in contracts:
            if isinstance(contract, Mapping) and isinstance(contract.get("type"), str):
                c = dict(contract)
                c["type"] = c["type"].lower()
                normalised.append(c)
            else:
                normalised.append(contract)
        out["apiContracts"] = normalised
    for name in _ID_ORDER_ARRAY_FIELDS:
        if isinstance(out.get(name), list):
            out[name] = _fill_item_ids(out[name], id_generator, order=True)
    reqs = out.get("requirements")
    if isinstance(reqs, list):
        new_reqs: list[Any] = []
        for req in reqs:
            if isinstance(req, Mapping):
                d = dict(req)
                if not d.get("id"):
                    d["id"] = _mint(id_generator)
                nested = d.get("acceptanceCriteria")
                if isinstance(nested, list):
                    d["acceptanceCriteria"] = _fill_item_ids(nested, id_generator, order=True)
                new_reqs.append(d)
            else:
                new_reqs.append(req)
        out["requirements"] = new_reqs
    return out


def build_create_persist(
    op: str,
    payload: Mapping[str, Any] | None,
    spec_full: SpecFull,
    *,
    id_generator: IdGenerator | None = None,
    clock: Any | None = None,
) -> CreatePersist | None:
    """Mint an id + assemble the full create columns for a CREATE op (``buildCreatePersist``).

    Numbering/order derive from the PRE-mutation ``spec_full`` counts. The cloud-only
    ``projectId`` denorm is dropped (reachable via ``specification_id``). Returns
    ``None`` for non-create ops (the caller falls back to :func:`extract_mutation_target`).
    """
    p = dict(payload) if payload else {}
    spec_id = spec_full.spec.id
    now = _now(clock)

    if op == "create_epic":
        n = len(spec_full.epics) + 1
        return CreatePersist(
            entity_type="epic",
            entity_id=_mint(id_generator),
            fields={
                "specification_id": spec_id,
                "epic_number": n,
                "order": n,
                "title": p.get("title", ""),
                "description": p.get("description", ""),
                "objective": p.get("objective", ""),
                "status": EpicStatus.TODO.value,
                "architecture": "",
                "planning_type": "planning",
                "requirements_covered": [],
                "created_at": now,
                "updated_at": now,
            },
        )

    if op == "create_ticket":
        epic_id = p.get("epicId", "")
        epic = next((e for e in spec_full.epics if e.id == epic_id), None)
        n = (len(epic.tickets) if epic is not None else 0) + 1
        return CreatePersist(
            entity_type="ticket",
            entity_id=_mint(id_generator),
            fields={
                "epic_id": epic_id,
                "ticket_number": n,
                "order": n,
                "title": p.get("title", ""),
                "description": p.get("description", ""),
                "status": TicketStatus.READY.value,
                "planning_type": "planning",
                # ticketType is CREATE-only (00c468fe): honour a 'verification' request so the
                # impl:verification ratio gate is satisfiable; default 'implementation'.
                "ticket_type": (
                    "verification" if p.get("ticketType") == "verification" else "implementation"
                ),
                "tags": [],
                "progress": 0,
                "created_at": now,
                "updated_at": now,
            },
        )

    if op == "create_blueprint":
        n = len(spec_full.blueprints) + 1
        return CreatePersist(
            entity_type="blueprint",
            entity_id=_mint(id_generator),
            fields={
                "specification_id": spec_id,
                "title": p.get("title", ""),
                "category": p.get("category", ""),
                "status": "draft",
                "order": n,
                "content": p.get("content", ""),
                "created_at": now,
                "updated_at": now,
            },
        )

    return None


#: ``update_ticket`` child-array field → (child table, deterministic-id infix).
_SNIPPET_DEFAULT_LANGUAGE = "typescript"


def _decompose_update_ticket(ticket_id: str, fields: Mapping[str, Any]) -> OpWriteItems:
    """Decompose ``update_ticket``'s child-backed arrays into ``RelatedReplace`` items.

    Each array (even empty) REPLACES the full child set for the ticket (the executor
    deletes the rows that dropped out). The decomposed arrays are STRIPPED off the flat
    ticket columns; the residual flat columns become ``fields_override``. Child ids are
    deterministic so a re-send overwrites in place.
    """
    extra: list[WritePlanItem] = []
    flat: dict[str, Any] = dict(fields)
    decomposed = False

    def replace(table: str, rows: list[dict[str, Any]]) -> None:
        nonlocal decomposed
        decomposed = True
        items = [
            {k: v for k, v in {"ticket_id": ticket_id, **row}.items() if v is not None}
            for row in rows
        ]
        extra.append(
            RelatedReplace(
                table=table, parent_field="ticket_id", parent_id=ticket_id, items=items
            )
        )

    # grouped testSpecification → TicketTest rows (testTypes) + flat
    # qualityGates / testCommands / coverageTarget hoisted onto the ticket columns.
    ts = fields.get("testSpecification")
    if isinstance(ts, Mapping):
        flat.pop("testSpecification", None)
        if isinstance(ts.get("qualityGates"), list):
            flat["qualityGates"] = ts["qualityGates"]
        if isinstance(ts.get("testCommands"), list):
            flat["testCommands"] = ts["testCommands"]
        coverage = ts.get("coverageTarget")
        if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
            flat["coverageTarget"] = coverage
        test_types = ts.get("testTypes")
        if isinstance(test_types, list):
            replace(
                "ticket_tests",
                [
                    {"id": f"{ticket_id}-tt-{t}", "test_type": t, "order": i}
                    for i, t in enumerate(
                        x for x in test_types if isinstance(x, str) and x
                    )
                ],
            )

    ac = fields.get("acceptanceCriteria")
    if isinstance(ac, list):
        flat.pop("acceptanceCriteria", None)
        replace(
            "acceptance_criteria",
            [
                {
                    "id": f"{ticket_id}-ac-{i}",
                    "given": (o := c if isinstance(c, Mapping) else {}).get("given", ""),
                    "when": o.get("when", ""),
                    "then": o.get("then", ""),
                    "order": i + 1,
                }
                for i, c in enumerate(ac)
            ],
        )

    steps = fields.get("implementationSteps")
    if isinstance(steps, list):
        flat.pop("implementationSteps", None)
        replace(
            "implementation_steps",
            [
                {
                    "id": f"{ticket_id}-is-{i}",
                    "text": s
                    if isinstance(s, str)
                    else (s.get("text", "") if isinstance(s, Mapping) else ""),
                    "order": i + 1,
                }
                for i, s in enumerate(steps)
            ],
        )

    code_snippets = fields.get("codeSnippets")
    if isinstance(code_snippets, list):
        flat.pop("codeSnippets", None)
        replace(
            "code_snippets",
            [
                {
                    "id": f"{ticket_id}-cs-{i}",
                    "language": (o := c if isinstance(c, Mapping) else {}).get(
                        "language", _SNIPPET_DEFAULT_LANGUAGE
                    ),
                    "description": o.get("description"),
                    "content": o.get("content", ""),
                    "order": i,
                }
                for i, c in enumerate(code_snippets)
            ],
        )

    type_snippets = fields.get("typeSnippets")
    if isinstance(type_snippets, list):
        flat.pop("typeSnippets", None)
        replace(
            "type_snippets",
            [
                {
                    "id": f"{ticket_id}-ts-{i}",
                    "language": (o := c if isinstance(c, Mapping) else {}).get(
                        "language", _SNIPPET_DEFAULT_LANGUAGE
                    ),
                    "description": o.get("description"),
                    "content": o.get("content", ""),
                    "order": i,
                }
                for i, c in enumerate(type_snippets)
            ],
        )

    blueprint_refs = fields.get("blueprintReferences")
    if isinstance(blueprint_refs, list):
        flat.pop("blueprintReferences", None)
        replace(
            "ticket_blueprint_refs",
            [
                {
                    "id": f"{ticket_id}-br-{i}",
                    "blueprint_id": (o := r if isinstance(r, Mapping) else {}).get(
                        "blueprintId", ""
                    ),
                    "context": o.get("context"),
                    "section": o.get("section"),
                }
                for i, r in enumerate(blueprint_refs)
            ],
        )

    # filesToBe* all live in TicketFileChange (discriminated by kind) — collect across
    # the four arrays and replace the whole set in one shot.
    file_fields = (
        ("filesToBeCreated", "toBeCreated"),
        ("filesToBeModified", "toBeModified"),
        ("filesToBeDeleted", "toBeDeleted"),
        ("filesToBeReferenced", "toBeReferenced"),
    )
    if any(isinstance(fields.get(name), list) for name, _ in file_fields):
        rows: list[dict[str, Any]] = []
        for name, kind in file_fields:
            arr = fields.get(name)
            flat.pop(name, None)
            if isinstance(arr, list):
                rows.extend(
                    {"id": f"{ticket_id}-fc-{kind}-{i}", "path": path, "kind": kind}
                    for i, path in enumerate(arr)
                    if isinstance(path, str)
                )
        replace("ticket_file_changes", rows)

    if decomposed:
        return OpWriteItems(extra_items=extra, fields_override=flat)
    return OpWriteItems()


def build_op_write_items(op: str, payload: Mapping[str, Any] | None) -> OpWriteItems:
    """Op-specific WritePlan items beyond the single ``SpecMutation`` (``buildOpWriteItems``).

    * delete ops → a single ``EntityDelete`` (FK ``ON DELETE CASCADE`` drops the subtree).
    * ``create_dependencies`` → ``RelatedPut`` ``ticket_dependencies`` rows with the
      deterministic edge id ``f"{from}--requires--{to}"`` (idempotent upsert).
    * ``delete_dependencies`` → ``RelatedDelete`` by edge id.
    * ``update_ticket`` → child-array decompose (:func:`_decompose_update_ticket`).
    """
    p = dict(payload) if payload else {}

    if op == "delete_epic":
        return OpWriteItems(delete_items=[EntityDelete("epic", p["id"])])
    if op == "delete_ticket":
        return OpWriteItems(delete_items=[EntityDelete("ticket", p["id"])])
    if op == "delete_blueprint":
        blueprint_id = p.get("id") or p.get("blueprintId") or ""
        return OpWriteItems(delete_items=[EntityDelete("blueprint", str(blueprint_id))])

    if op == "create_dependencies":
        edges = p.get("dependencies") or []
        extra: list[WritePlanItem] = []
        for e in edges:
            if not isinstance(e, Mapping):
                continue
            frm = e.get("fromTicketId")
            to = e.get("toTicketId")
            if isinstance(frm, str) and isinstance(to, str):
                extra.append(
                    RelatedPut(
                        table="ticket_dependencies",
                        item={
                            "id": f"{frm}--requires--{to}",
                            "ticket_id": frm,
                            "depends_on_id": to,
                            "type": "requires",
                        },
                    )
                )
        return OpWriteItems(extra_items=extra)

    if op == "delete_dependencies":
        ids = p.get("dependencyIds") or []
        return OpWriteItems(
            extra_items=[
                RelatedDelete(table="ticket_dependencies", key={"id": i})
                for i in ids
                if isinstance(i, str)
            ]
        )

    if op in ("link_blueprint_to_tickets", "unlink_blueprint_to_tickets"):
        # MB.2 — a real relational write: one ticket_blueprint_refs join row per target
        # ticket, deterministic id f"{ticket_id}-br-{blueprint_id}" (idempotent link /
        # keyed unlink), instead of the old no-op single-entity mutation. Without this the
        # cross_validation blueprint-coverage check never accumulates and the loop stalls.
        blueprint_id = p.get("blueprintId")
        ticket_ids = p.get("ticketIds") or []
        if not isinstance(blueprint_id, str):
            return OpWriteItems()
        linking = op == "link_blueprint_to_tickets"
        link_items: list[WritePlanItem] = []
        for tid in ticket_ids:
            if not isinstance(tid, str):
                continue
            ref_id = f"{tid}-br-{blueprint_id}"
            if linking:
                link_items.append(
                    RelatedPut(
                        table="ticket_blueprint_refs",
                        item={"id": ref_id, "ticket_id": tid, "blueprint_id": blueprint_id},
                    )
                )
            else:
                link_items.append(
                    RelatedDelete(table="ticket_blueprint_refs", key={"id": ref_id})
                )
        return OpWriteItems(extra_items=link_items)

    if op == "update_ticket":
        fields = p.get("fields")
        ticket_id = p.get("id")
        if isinstance(fields, Mapping) and isinstance(ticket_id, str):
            return _decompose_update_ticket(ticket_id, fields)
        return OpWriteItems()

    return OpWriteItems()


def extract_mutation_target(
    op: str, payload: Mapping[str, Any] | None, specification_id: str
) -> tuple[EntityType, str]:
    """Resolve the (entity_type, entity_id) a non-create op mutates (``extractMutationTarget``)."""
    p = payload or {}
    # MB.2 — link/unlink are RELATIONAL ops that mutate no single entity's fields; their
    # target must resolve to the always-existing spec BEFORE the substring fall-throughs
    # below (``'ticket' in 'link_blueprint_to_TICKETS'`` would otherwise build a phantom
    # Ticket keyed by the blueprintId → a NOT-NULL IntegrityError on every valid link id).
    if op in ("link_blueprint_to_tickets", "unlink_blueprint_to_tickets"):
        return ("spec", specification_id)

    raw_id = p.get("id")
    if isinstance(raw_id, str):
        entity_id = raw_id
    else:
        raw_bp = p.get("blueprintId")
        entity_id = raw_bp if isinstance(raw_bp, str) else "unknown"

    if op == "update_spec":
        return ("spec", specification_id)
    if "epic" in op:
        return ("epic", entity_id)
    if "ticket" in op:
        return ("ticket", entity_id)
    if "blueprint" in op:
        return ("blueprint", entity_id)
    return ("spec", specification_id)


def resolve_aps_mutation(
    op: str,
    payload: Mapping[str, Any] | None,
    spec_full: SpecFull,
    *,
    id_generator: IdGenerator | None = None,
    clock: Any | None = None,
) -> ApsMutation:
    """Combine create-persist + op-write-items into the :class:`ApsMutation` (A1 §1.4 step 11/12).

    CREATE ops mint a real id + the full create columns; other ops resolve the target
    via :func:`extract_mutation_target` and carry the payload's ``fields``. A decomposed
    ``update_ticket`` overrides those flat fields with its residual columns.
    """
    create_persist = build_create_persist(
        op, payload, spec_full, id_generator=id_generator, clock=clock
    )
    if create_persist is not None:
        entity_type: EntityType = create_persist.entity_type
        entity_id = create_persist.entity_id
        mutation_fields: dict[str, Any] = create_persist.fields
    else:
        entity_type, entity_id = extract_mutation_target(op, payload, spec_full.spec.id)
        raw_fields = (payload or {}).get("fields")
        mutation_fields = dict(raw_fields) if isinstance(raw_fields, Mapping) else {}

    op_items = build_op_write_items(op, payload)
    effective_fields = (
        op_items.fields_override
        if op_items.fields_override is not None
        else mutation_fields
    )
    # MB.1.1 — mint schema-required id/order into spec/epic content arrays before the
    # write (tickets are handled by _decompose_update_ticket, which already stamps ids).
    if entity_type in ("spec", "epic"):
        effective_fields = ensure_item_ids(effective_fields, id_generator=id_generator)
    return ApsMutation(
        entity_type=entity_type,
        entity_id=entity_id,
        fields=dict(effective_fields),
        delete_items=op_items.delete_items,
        extra_items=op_items.extra_items,
    )


# --------------------------------------------------------------------------- #
# SPS write plans                                                             #
# --------------------------------------------------------------------------- #


def build_sps_create_write_plan(
    *,
    spec_id: str,
    spec_status: SpecStatus | str,
    session: Mapping[str, Any],
    action: Mapping[str, Any],
) -> WritePlan:
    """SPS create: ``[SpecMutation status='planning' (draft only), SessionCreate, ActionAppend]``.

    The spec is flipped to ``planning`` ONLY when it was ``draft`` (the auto-transition);
    an already-``planning`` spec carries no ``SpecMutation``.
    """
    items: list[WritePlanItem] = []
    if _v(spec_status) == SpecStatus.DRAFT.value:
        items.append(SpecMutation("spec", spec_id, {"status": SpecStatus.PLANNING.value}))
    items.append(SessionCreate(dict(session)))
    items.append(ActionAppend(dict(action)))
    return WritePlan(
        items, description=f"SPS CREATE: spec={spec_id}, session={session.get('id')}"
    )


def build_sps_resume_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    fresh_validator_output: ValidatorOutput | None = None,
    fresh_validated_at: str | None = None,
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """SPS resume-active: ``[SessionUpdate, ActionAppend]``.

    The fresh validator output (``last_validator_output`` / ``last_score`` /
    ``last_gate_result`` / ``last_validated_at``) is persisted ONLY when a fresh
    validate happened (the cache-null fallback); an idempotent cache-hit resume just
    refreshes the guidance snapshot + ``last_action_at``.
    """
    fields = _guidance_fields(process_guidance, lifecycle_planning_guidance)
    fields["last_action_at"] = action.get("created_at")
    if fresh_validator_output is not None:
        fields["last_validator_output"] = _serialize_validator_output(fresh_validator_output)
        fields["last_score"] = fresh_validator_output.local_score
        fields["last_gate_result"] = fresh_validator_output.gate_result
        fields["last_validated_at"] = fresh_validated_at
    return WritePlan(
        [SessionUpdate(session_id, fields), ActionAppend(dict(action))],
        description=f"SPS RESUME: session={session_id}",
    )


def _audit_only_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None,
    lifecycle_planning_guidance: Mapping[str, Any] | None,
    description: str,
) -> WritePlan:
    """The shared denial/audit-only shape: ``[ActionAppend, SessionUpdate(guidance)]``.

    No entity mutation and no semantic session change — only the audit row + the
    latest-only guidance snapshot + an ``last_action_at`` heartbeat.
    """
    fields = _guidance_fields(process_guidance, lifecycle_planning_guidance)
    fields["last_action_at"] = action.get("created_at")
    return WritePlan(
        [ActionAppend(dict(action)), SessionUpdate(session_id, fields)],
        description=description,
    )


def build_sps_deny_awaiting_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """SPS denied (called on an ``awaiting_human_review`` session): audit-only."""
    return _audit_only_plan(
        session_id=session_id,
        action=action,
        process_guidance=process_guidance,
        lifecycle_planning_guidance=lifecycle_planning_guidance,
        description=f"SPS denied (not-for-awaiting): session={session_id}",
    )


# --------------------------------------------------------------------------- #
# APS write plans                                                             #
# --------------------------------------------------------------------------- #


def build_aps_denied_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """APS denied: audit-only ``ActionAppend`` (the denial), no entity mutation."""
    return _audit_only_plan(
        session_id=session_id,
        action=action,
        process_guidance=process_guidance,
        lifecycle_planning_guidance=lifecycle_planning_guidance,
        description=f"APS denied: session={session_id}",
    )


def build_aps_success_write_plan(
    *,
    session_id: str,
    mutation: ApsMutation,
    action: Mapping[str, Any],
    gate_result: Literal["pass", "fail"],
    validator_output: ValidatorOutput,
    entity_score_writes: Sequence[EntityScoreWrite],
    prev_session_status: Literal["active", "awaiting_human_review"],
    actor: ActorType | str = ActorType.AGENT,
    rollback: ApsRollback | None = None,
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """APS success — the big one.

    Items: the primary op mutation (``SpecMutation`` or the ``delete_items``), a
    ``SessionUpdate``, an optional rollback ``RecordTransition`` (late op), the
    ``ActionAppend``, the op ``extra_items`` (dependency / ``RelatedReplace`` rows), and
    the gate's entity-score writes as ``ScoreDatapointAppend`` items (NO row fusion, NO
    ``EntityScoreUpdate``).

    The ``SessionUpdate`` persists ``last_validator_output`` + ``last_score`` +
    ``last_gate_result`` + ``last_validated_at`` (the A1 §3.3 cache-divergence fix — the
    session is the single source of truth). M11.1 actor-conditional status: an awaiting
    session flips to ``active`` UNLESS the editor is ``human`` (a human edit re-scores in
    place and stays ``awaiting_human_review``). A late op rolls ``current_phase`` back to
    the rollback's effective (native) phase.
    """
    performed_at = action.get("created_at")
    trigger_action_id = action["id"]

    session_fields = _guidance_fields(process_guidance, lifecycle_planning_guidance)
    session_fields["last_gate_result"] = gate_result
    session_fields["last_validated_at"] = performed_at
    session_fields["last_action_at"] = performed_at
    session_fields["last_validator_output"] = _serialize_validator_output(validator_output)
    session_fields["last_score"] = validator_output.local_score
    if _v(actor) != ActorType.HUMAN.value and prev_session_status == "awaiting_human_review":
        session_fields["status"] = PlanningSessionStatus.ACTIVE.value
    if rollback is not None:
        session_fields["current_phase"] = _v(rollback.effective_phase)

    primary_items: list[WritePlanItem] = (
        list(mutation.delete_items)
        if mutation.delete_items
        else [SpecMutation(mutation.entity_type, mutation.entity_id, dict(mutation.fields))]
    )

    core_items: list[WritePlanItem] = [
        *primary_items,
        SessionUpdate(session_id, session_fields),
        *([RecordTransition(dict(rollback.transition))] if rollback is not None else []),
        ActionAppend(dict(action)),
        *mutation.extra_items,
    ]

    items = [*core_items, *_score_datapoints(entity_score_writes, trigger_action_id)]
    return WritePlan(items, description=f"APS success: session={session_id}")


# --------------------------------------------------------------------------- #
# CPS write plans                                                             #
# --------------------------------------------------------------------------- #


def build_cps_denied_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """CPS denied (gate not passed / session not active): audit-only."""
    return _audit_only_plan(
        session_id=session_id,
        action=action,
        process_guidance=process_guidance,
        lifecycle_planning_guidance=lifecycle_planning_guidance,
        description=f"CPS denied: session={session_id}",
    )


def build_cps_success_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    transition: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
    lifecycle_planning_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """CPS success: ``[SessionUpdate status='awaiting_human_review', RecordTransition, ActionAppend]``.

    CPS does NOT advance the phase — it parks the session for human review (the
    ``RecordTransition`` is same-phase, trigger ``ai_agent``).
    """
    fields = _guidance_fields(process_guidance, lifecycle_planning_guidance)
    fields["status"] = PlanningSessionStatus.AWAITING_HUMAN_REVIEW.value
    # #8 — CPS success is only reachable past the gate-currently-passing check, so the
    # gate is PASS at park time. Persist it (+ last_validated_at) so approve_handover's
    # gate-on-pass guard doesn't refuse a handover this CPS just green-lit when the
    # prior APS left last_gate_result='fail'.
    fields["last_gate_result"] = "pass"
    fields["last_validated_at"] = action.get("created_at")
    fields["last_action_at"] = action.get("created_at")
    return WritePlan(
        [
            SessionUpdate(session_id, fields),
            RecordTransition(dict(transition)),
            ActionAppend(dict(action)),
        ],
        description=f"CPS success: session={session_id}",
    )


# --------------------------------------------------------------------------- #
# Handover write plans                                                        #
# --------------------------------------------------------------------------- #


def build_approve_handover_write_plan(
    *,
    session_id: str,
    spec_id: str,
    transitioned_phase: PlanningPhase | str,
    is_terminal: bool,
    now: datetime | str,
    actions: Sequence[Mapping[str, Any]],
    transition: Mapping[str, Any] | None = None,
) -> WritePlan:
    """approveHandover.

    TERMINAL (``cross_validation``): ``SpecMutation`` spec → ``ready`` + ``SessionUpdate``
    → ``closed`` (with ``closed_at``); the only place the spec becomes ``ready``.
    NON-TERMINAL: ``SessionUpdate`` → ``active`` with ``current_phase = next_phase`` +
    a ``RecordTransition`` (trigger ``human_approve``). Both then append the audit rows
    (``[phase_complete, session_closed]`` terminal / ``[phase_advance, human_approve]``).
    """
    now_iso_str = _iso(now)
    items: list[WritePlanItem] = []

    if is_terminal:
        items.append(SpecMutation("spec", spec_id, {"status": SpecStatus.READY.value}))
        items.append(
            SessionUpdate(
                session_id,
                {
                    "status": PlanningSessionStatus.CLOSED.value,
                    "closed_at": now_iso_str,
                    "last_transition_trigger": TransitionTrigger.HUMAN_APPROVE.value,
                    "last_transition_at": now_iso_str,
                    "last_action_at": now_iso_str,
                },
            )
        )
        description = f"approveHandover TERMINAL (closed): session={session_id}"
    else:
        items.append(
            SessionUpdate(
                session_id,
                {
                    "status": PlanningSessionStatus.ACTIVE.value,
                    "current_phase": _v(transitioned_phase),
                    "last_transition_trigger": TransitionTrigger.HUMAN_APPROVE.value,
                    "last_transition_at": now_iso_str,
                    "last_action_at": now_iso_str,
                },
            )
        )
        if transition is not None:
            items.append(RecordTransition(dict(transition)))
        description = (
            f"approveHandover advance to {_v(transitioned_phase)}: session={session_id}"
        )

    items.extend(ActionAppend(dict(a)) for a in actions)
    return WritePlan(items, description=description)


def build_reject_handover_with_feedback_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    feedback: Mapping[str, Any],
    now: datetime | str,
) -> WritePlan:
    """rejectHandoverWithFeedback: ``SessionUpdate`` → ``active`` + stash
    ``pending_human_feedback`` (``{content, recorded_at, …}``) for one-shot delivery;
    ``last_transition_trigger = 'human_reject_with_feedback'``. Phase unchanged."""
    now_iso_str = _iso(now)
    fields: dict[str, Any] = {
        "status": PlanningSessionStatus.ACTIVE.value,
        "pending_human_feedback": dict(feedback),
        "last_transition_trigger": TransitionTrigger.HUMAN_REJECT_WITH_FEEDBACK.value,
        "last_transition_at": now_iso_str,
        "last_action_at": now_iso_str,
    }
    return WritePlan(
        [SessionUpdate(session_id, fields), ActionAppend(dict(action))],
        description=f"rejectHandoverWithFeedback: session={session_id}",
    )


def build_reject_handover_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    now: datetime | str,
) -> WritePlan:
    """rejectHandover (no feedback): ``SessionUpdate`` → ``active``,
    ``last_transition_trigger = 'human_reject_no_feedback'``. No feedback stash. Phase
    unchanged."""
    now_iso_str = _iso(now)
    fields: dict[str, Any] = {
        "status": PlanningSessionStatus.ACTIVE.value,
        "last_transition_trigger": TransitionTrigger.HUMAN_REJECT_NO_FEEDBACK.value,
        "last_transition_at": now_iso_str,
        "last_action_at": now_iso_str,
    }
    return WritePlan(
        [SessionUpdate(session_id, fields), ActionAppend(dict(action))],
        description=f"rejectHandover (no feedback): session={session_id}",
    )


# --------------------------------------------------------------------------- #
# get_planning_status write plans                                            #
# --------------------------------------------------------------------------- #


def build_gps_feedback_consume_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
) -> WritePlan:
    """get_planning_status (human_feedback_received): atomically clear
    ``pending_human_feedback`` (= ``None``) with the audit append (one-shot delivery).
    NEVER re-runs the validator."""
    fields: dict[str, Any] = {"pending_human_feedback": None}
    if process_guidance is not None:
        fields["last_process_guidance"] = dict(process_guidance)
    fields["last_action_at"] = action.get("created_at")
    return WritePlan(
        [SessionUpdate(session_id, fields), ActionAppend(dict(action))],
        description=f"get_planning_status (feedback consumed): session={session_id}",
    )


def build_gps_read_only_write_plan(
    *,
    session_id: str,
    action: Mapping[str, Any],
    process_guidance: Mapping[str, Any] | None = None,
    bump_last_read_at: bool = False,
    now: datetime | str | None = None,
) -> WritePlan:
    """get_planning_status (read-only): append the audit row + persist the guidance
    snapshot; optionally bump ``last_read_at`` (the one-shot transition-announcement
    variants). NEVER re-runs the validator."""
    fields: dict[str, Any] = {}
    if process_guidance is not None:
        fields["last_process_guidance"] = dict(process_guidance)
    fields["last_action_at"] = action.get("created_at")
    if bump_last_read_at:
        fields["last_read_at"] = _iso(now) if now is not None else now_iso()
    return WritePlan(
        [SessionUpdate(session_id, fields), ActionAppend(dict(action))],
        description=f"get_planning_status (read-only): session={session_id}",
    )
