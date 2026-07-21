export const meta = {
  name: 'lifecycle-parity-audit',
  description:
    'Exhaustive parity audit of the specsmither-lifecycle pure core against the @specforge/lifecycle reference',
  whenToUse:
    'After syncing specsmither-lifecycle to a new @specforge/lifecycle version, or before cutting a release. Pass {baseline, head} to scope the upstream accounting.',
  phases: [
    { title: 'Static', detail: 'module / symbol / vocab / state-machine / precheck / write-plan coverage' },
    { title: 'Verbs', detail: 'differential over the pure state → {response, WritePlan} verbs' },
    { title: 'Upstream', detail: 'every upstream hunk in range accounted for' },
    { title: 'Verify', detail: 'two adversarial lenses per finding' },
    { title: 'Synthesis', detail: 'ranked report' },
  ],
}

// ---------------------------------------------------------------- parameters
const PY = (args && args.pyRoot) || '/opt/source/specsmither'
const CORE = `${PY}/packages/specsmither-lifecycle/src/specsmither`
const TS = (args && args.tsRoot) || '/opt/source/specforge'
const L = `${TS}/packages/lifecycle`
const BASE = (args && args.baseline) || 'HEAD~1'
const HEAD = (args && args.head) || 'HEAD'

// Shared preamble. Without this the finders drown the report in false positives:
// the port diverges from the reference on purpose in several documented ways.
const GROUND = `
You are auditing PARITY between two implementations of the same planning lifecycle:

  REFERENCE (TypeScript, authoritative): ${L}/src
  PORT      (Python, pure core):         ${CORE}/lifecycle  (+ ${CORE}/domain, ${CORE}/ids.py)

The port is the PURE planning core: the verbs are pure \`(state, ports) -> {response, WritePlan}\`
functions that read via injected ports and describe mutations as a WritePlan — they never
persist. The consuming \`specsmither\` product supplies the SQLite executor, stores, MCP and TUI.
Your job is to find where the PORT'S BEHAVIOUR diverges from the reference, or where upstream
lifecycle behaviour was never ported at all.

TOOLING
- Run Python:  cd ${PY} && uv run python ...   (or ${PY}/.venv/bin/python)
- Run TS:      cd ${TS} && TSX_TSCONFIG_PATH=${TS}/tsconfig.base.json npx tsx <script>
- Inspect upstream history: cd ${TS} && git show <rev>:packages/lifecycle/src/<file>
- The pure verbs are testable WITHOUT a database: see ${PY}/tests/pure_harness.py and
  ${PY}/tests/test_lifecycle_pure_core.py — they drive each verb as a pure function and
  normalise the WritePlan to a JSON dict. Reuse that shape to compare against the reference.

PREFER EXECUTION OVER READING. A claim produced by running both sides and diffing output is
worth more than one produced by reading two files side by side. Reading finds candidates;
running proves them.

INTENTIONAL DIVERGENCES — these are NOT findings. Do not report them:
1. Persistence. The reference persists via DynamoDB/AppSync/Lambda resolvers (executor,
   aggregate-projector, GSI cascade-readers, stream projection). The port persists via
   SQLite/SQLAlchemy + a WritePlan executor, and cascade-deletes through FK ON DELETE
   CASCADE rather than GSI cascade-readers. Divergence in HOW mutations are persisted is
   intentional — only the WritePlan CONTRACT (which mutations are described, and the
   response) must match. Anything under the reference's resolvers/backend/Lambda layer that
   has no Python counterpart is out of scope unless it changes the WritePlan or a verb response.
2. Validator seam. The port validates via \`crucible\` (crucible-forge); the reference uses
   \`@specforge/validator\`. Same behaviour, different package — not a finding.
3. Guidance PROSE. The port owns its agent-facing guidance catalogs and process-guidance
   wording as its own product voice; the composition LOGIC (which blocks compose, in what
   order, under which variant) must match, but byte-identical prose is NOT required. Only a
   LOGIC/variant-selection divergence is a finding, not a reworded sentence.
4. The pure-core split. The port ships \`specsmither.lifecycle\` as a pure, dependency-light
   distribution (crucible-forge + pydantic + python-ulid + pyyaml only); the reference is one
   package. Module layout consolidates — map by BEHAVIOUR, not filename.
5. snake_case, dataclasses/pydantic instead of interfaces, frozenset instead of ReadonlySet,
   Literal[...] unions instead of string-literal unions — idiomatic translation, not divergence.
6. Python being MORE null-tolerant (\`x or []\`) where the reference would throw on malformed
   input. Only a finding if it changes output for VALID input.
7. Differential harnesses / fixtures generated from the private reference are gitignored on
   purpose.
8. Work-session and review lifecycles are frozen-zone stubs in the port (planned later). The
   reference's \`work\`/\`review\` surfaces are OUT OF SCOPE — only the planning lifecycle counts.
9. Audit-row ids are monotonic ULIDs on BOTH sides.

A REAL finding is one of:
- A verb that, for VALID state, returns a different WritePlan (different item kinds/targets/
  fields/order) or a different response (outcome, phase, status, findings, guidance variant).
- A precheck/deny-gate that accepts what the reference denies (or vice versa), or returns a
  different deny code.
- A state-machine transition, phase-gate, or operation-classification that differs.
- Upstream lifecycle behaviour in the audited range that was never ported.
- A public surface (\`specsmither.lifecycle\`) that cannot actually be used, or that lies about itself.
- Data (operation contract, registry metadata, vocabularies) that disagrees with the reference.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    checkedWhat: {
      type: 'string',
      description: 'One paragraph: what you actually ran/compared, so a reader can judge coverage.',
    },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
          pyRef: { type: 'string', description: 'python file:line' },
          tsRef: { type: 'string', description: 'reference file:line' },
          evidence: {
            type: 'string',
            description:
              'Concrete proof. Ideally the two outputs that differ, verbatim. State if you only read code.',
          },
          provedByExecution: { type: 'boolean' },
          suggestedFix: { type: 'string' },
        },
        required: ['title', 'severity', 'evidence', 'provedByExecution', 'suggestedFix'],
      },
    },
  },
  required: ['checkedWhat', 'findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    refuted: { type: 'boolean' },
    reasoning: { type: 'string' },
    correctedSeverity: { type: 'string', enum: ['critical', 'high', 'medium', 'low', 'nit'] },
  },
  required: ['refuted', 'reasoning'],
}

// ------------------------------------------------------------- static sweeps
// Each looks at parity from an angle the others are blind to.
const STATIC = [
  {
    key: 'modules',
    prompt: `Coverage sweep, BY MODULE. Enumerate every non-test .ts under ${L}/src (planning/*,
ports, config, freeze, adapters) and determine whether its behaviour exists in the port under
${CORE}/lifecycle. The port consolidates files, so map by BEHAVIOUR, not filename. Report any
reference module whose logic you cannot locate in Python, and the reverse (Python implementing
something the reference dropped). Remember the resolvers/backend layer is intentionally not ported.`,
  },
  {
    key: 'symbols',
    prompt: `Coverage sweep, BY EXPORTED SYMBOL. Extract every symbol exported from
${L}/src/index.ts. For each planning-lifecycle symbol, find the Python equivalent and check it is
reachable from \`import specsmither.lifecycle\` (verify by constructing an import in a throwaway
\`uv run python -c\`). A verb exported without the types needed to build its arguments counts as
unusable.`,
  },
  {
    key: 'vocab',
    prompt: `Coverage sweep, BY VOCABULARY. Compare the string-literal unions/enums: planning
operation names (${L}/src vs ${CORE}/lifecycle/operations_registry.py PlanningOperationName), the
planning phases (${CORE}/domain), precheck deny codes, and the WritePlan item kinds
(${CORE}/lifecycle/write_plan_types.py). Compare MEMBERS and, where the reference relies on it,
ORDER. Print both sides.`,
  },
  {
    key: 'state-machine',
    prompt: `Behavioural parity, STATE MACHINE. Compare the phase transition table + gate evaluator
(${L}/src/planning/state-machine.ts, compositions/phase-gate-evaluator.ts) against
${CORE}/lifecycle/state_machine.py and ${CORE}/lifecycle/gate.py. Verify: allowed transitions per
phase, the ALL-PASS vs mean gate decision, phase advance/rollback triggers, and operation
classification (native/late/forbidden) in operations_registry. Build inputs that land on each
transition boundary and compare the decision.`,
  },
  {
    key: 'prechecks',
    prompt: `Behavioural parity, DENY-GATE. For each pre-check under
${L}/src/planning/pre-checks/*.ts compare accept/deny conditions AND the deny code against
${CORE}/lifecycle/prechecks/*.py (schema_validate, operation_allowed, count_bounds, cascade_rules,
cross_cut_references, blueprint_epic_ratio, gate_currently_passing, spec_status_check,
dependencies_batch, entity_refs_exist, ...). Drive payloads that sit just inside and just outside
each rule through both and compare the PreCheckResult.`,
  },
  {
    key: 'write-plan',
    prompt: `Behavioural parity, WRITE-PLAN. The action verb decomposes an operation into WritePlan
items. Compare the op → WritePlan mapping in ${L}/src (the APS write-plan builder) against
${CORE}/lifecycle/write_plan.py: create/update/delete items, the update_ticket child-array
decompose (acceptance criteria, steps, filesToBe*, testTypes, blueprint refs, code/type snippets),
the REPLACE-not-upsert semantics for child arrays, entity cascade-delete, and the dependency
edge primitives. Compare the normalised WritePlan (kinds + targets + fields + order), not the
persistence mechanism.`,
  },
  {
    key: 'catalog-contract',
    prompt: `Data parity, OPERATION CONTRACT. Compare the canonical per-operation payload contract
and the catalog-parity guard: ${L}/src/planning/pre-checks/schema-zod.ts
(PLANNING_OPERATION_CONTRACT / checkPlanningCatalogParity) against
${CORE}/lifecycle/prechecks/schema_validate.py (PLANNING_OPERATION_CONTRACT /
check_planning_catalog_parity). Compare, per operation, the required fields and the array-item
required fields. Load both and diff the resulting structures.`,
  },
  {
    key: 'guidance-logic',
    prompt: `Behavioural parity, GUIDANCE COMPOSITION LOGIC (not prose). Compare which guidance
blocks compose and under which variant: ${L}/src/planning/process-guidance/* against
${CORE}/lifecycle/guidance/*. Verify variant selection (phase status variants, handover vs
feedback vs closed, recommended-moves derivation via the operation mapper). Report a LOGIC or
variant-selection divergence; do NOT report reworded prose — the port owns its wording.`,
  },
]

const VERBS = [
  'start_planning_session',
  'action_planning_session',
  'complete_planning_session',
  'approve_handover',
  'reject_handover',
  'reject_handover_with_feedback',
  'inspect_planning_session',
]

const LENSES = [
  {
    key: 'fidelity',
    ask: `Is this actually a DIVERGENCE FROM THE REFERENCE, or is it an intentional/idiomatic
translation (persistence layer, validator package, guidance prose, module layout)? Re-read the
reference at the cited location and check the intentional-divergence list. If the reference does
the same thing, or the difference cannot change a WritePlan/response for valid input, REFUTE.`,
  },
  {
    key: 'observable',
    ask: `Does this change OBSERVABLE OUTPUT for valid state — the WritePlan, the verb response,
or a deny code? Try to build concrete state that makes the two sides disagree, and RUN BOTH via
the pure harness. If you cannot produce disagreement, REFUTE — including when the reasoning
sounds right but no input exercises it.`,
  },
]

function verify(f, dim) {
  return parallel(
    LENSES.map((l) => () =>
      agent(
        `${GROUND}

A parity audit produced this candidate finding in the "${dim}" dimension. Your job is to REFUTE it.
Default to refuted=true when uncertain — a false finding costs more than a missed nit here,
because the maintainer will act on whatever survives.

  title:      ${f.title}
  severity:   ${f.severity}
  pyRef:      ${f.pyRef || '(none given)'}
  tsRef:      ${f.tsRef || '(none given)'}
  provedByExecution: ${f.provedByExecution}
  evidence:   ${f.evidence}

LENS — ${l.key}: ${l.ask}

Investigate independently. Do not trust the evidence as written; reproduce it.`,
        { label: `verify:${l.key}`, phase: 'Verify', schema: VERDICT_SCHEMA },
      ),
    ),
  ).then((vs) => ({ ...f, dimension: dim, verdicts: vs.filter(Boolean) }))
}

function fanVerify(res, dim) {
  const found = (res && res.findings) || []
  if (!found.length) return []
  return parallel(found.map((f) => () => verify(f, dim)))
}

// ------------------------------------------------------------------- run it
phase('Static')

const staticRuns = pipeline(
  STATIC,
  (d) =>
    agent(`${GROUND}\n\nDIMENSION: ${d.key}\n\n${d.prompt}`, {
      label: `static:${d.key}`,
      phase: 'Static',
      schema: FINDINGS_SCHEMA,
    }),
  (res, d) => fanVerify(res, d.key),
)

const verbRuns = pipeline(
  VERBS,
  (v) =>
    agent(
      `${GROUND}

DIMENSION: differential over the pure verb "${v}".

Drive THIS VERB as a pure function on both sides and compare its output:

1. Read ${PY}/tests/pure_harness.py + ${PY}/tests/test_lifecycle_pure_core.py — they build the
   pure ports (real projector + fakes + deterministic ids/clock) and normalise the WritePlan to a
   JSON dict. Reuse that harness.
2. Construct equivalent session/spec state on both sides and invoke "${v}". Vary: the current
   phase, the operation + payload (for action_planning_session — cover create/update/delete across
   epic/ticket/blueprint, dependencies, blueprint links, and the child-array update_ticket path),
   gate passing vs failing, and the handover pre-conditions (for approve/reject).
3. Compare the NORMALISED WritePlan (item kinds + targets + fields + order) AND the response
   (outcome, phase, status, findings, guidance variant) between the port and the reference.
4. On any mismatch, MINIMISE it to the smallest state that still diverges, and report that.

Write scratch files under a temp dir, not inside ${PY}. Report how many state variants you drove
and how many diverged; if zero diverged, say so plainly in checkedWhat — that is a valuable result.`,
      { label: `verb:${v}`, phase: 'Verbs', schema: FINDINGS_SCHEMA },
    ),
  (res, v) => fanVerify(res, `verb:${v}`),
)

const upstreamRuns = pipeline(
  ['hunks'],
  () =>
    agent(
      `${GROUND}

DIMENSION: upstream accounting.

Enumerate EVERY change to ${L}/src between ${BASE} and ${HEAD}, excluding __tests__:

  cd ${TS} && git diff ${BASE}..${HEAD} -- packages/lifecycle/src ':!*__tests__*'

For each distinct behavioural change, decide one of:
  (a) PORTED — point at the Python that implements it;
  (b) INTENTIONALLY OMITTED — cite the reason (the intentional-divergence list, e.g. it is
      resolvers/backend/AWS-only, or work/review lifecycle);
  (c) MISSED — no Python implements it and no rationale exists. THIS is a finding.

Be exhaustive and go hunk by hunk. A change buried inside an otherwise-ported file is the most
likely thing to have been missed. Report every (c), and in checkedWhat give the counts for
(a)/(b)/(c) so coverage is auditable.`,
      { label: 'upstream:hunks', phase: 'Upstream', schema: FINDINGS_SCHEMA },
    ),
  (res) => fanVerify(res, 'upstream'),
)

const [statics, verbs, upstreams] = await Promise.all([staticRuns, verbRuns, upstreamRuns])

const all = [...statics, ...verbs, ...upstreams].flat().filter(Boolean)

// Survives if at least one lens declined to refute it.
const survivors = all.filter((f) => (f.verdicts || []).some((v) => v && !v.refuted))
const killed = all.length - survivors.length

log(`${all.length} candidate findings → ${survivors.length} survived, ${killed} refuted`)

phase('Synthesis')

if (!survivors.length) {
  return {
    verdict: 'clean',
    candidates: all.length,
    refuted: killed,
    report:
      'No parity finding survived adversarial verification across static, verb and upstream sweeps.',
  }
}

const report = await agent(
  `${GROUND}

You are writing the FINAL REPORT of a lifecycle parity audit. ${all.length} candidate findings
were produced; ${survivors.length} survived two adversarial lenses each. Here they are as JSON:

${JSON.stringify(survivors, null, 2)}

Write a report in BRAZILIAN PORTUGUESE (pt-BR):
- Rank by real severity, most severe first. Re-judge severity yourself; the finders inflate.
- MERGE duplicates: the same root cause found by several dimensions is ONE entry.
- For each: what breaks, the concrete failure (state → wrong WritePlan/response), the file:line, and the fix.
- Separate "muda comportamento" from "cosmético/documentação" — the maintainer triages differently.
- Flag anything where both lenses disagreed with each other as uncertain, and say why.
- End with a short honest coverage statement: which dimensions proved things by EXECUTION vs
  only by reading, and what a reader should NOT conclude from this audit.

Be precise and skeptical. Do not pad. If something is a nit, call it a nit.`,
  { label: 'synthesis', phase: 'Synthesis' },
)

return { verdict: 'findings', candidates: all.length, refuted: killed, survivors: survivors.length, report }
