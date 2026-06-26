export const meta = {
  name: 'verify-milestone',
  description:
    'Verify a planner/ milestone doc is well-formed and implementable: it has an Objective, Scope (in/out), a work-breakdown where every work-item names a TS source-to-port (or "new") + a target Python module + an actionable note, plus Deliverables and Acceptance/verification. Read-only gate — emits a structured report, changes nothing.',
  whenToUse:
    'Run before starting a milestone. Pass the milestone id (e.g. "00", "0.1.0", "planning", "work", "assay") as args. Confirms the markdown under planner/ is complete and unambiguous enough to execute, and that it respects the SpecSmither cross-cutting invariants (planner/README.md).',
  phases: [
    { title: 'Resolve', detail: 'Locate the milestone markdown under planner/ by id/slug' },
    { title: 'Verify', detail: 'Per-work-item structural check (source ref, target module, note) + Acceptance/Deliverables' },
    { title: 'Audit', detail: 'Adversarial completeness panel — fidelity to invariants, coverage, testability/source-trace' },
  ],
}

// --- args: "00" | "planning" | { milestone: "0.1.0" } -------------------------
const MILESTONE = typeof args === 'string' ? args : args && args.milestone
if (!MILESTONE) {
  throw new Error(
    'verify-milestone needs a milestone id. Invoke with args: "00" (or "0.1.0" / "planning" / "work" / "assay") or args: { milestone: "00" }.',
  )
}

const DIR = 'planner'
const INVARIANTS = `${DIR}/README.md` // cross-cutting invariants + reference corrections

const RESOLVE_SCHEMA = {
  type: 'object',
  required: ['found'],
  additionalProperties: false,
  properties: {
    found: { type: 'boolean' },
    path: { type: 'string', description: 'Absolute path of the single best-matching milestone file' },
    candidates: { type: 'array', items: { type: 'string' }, description: 'All planner/*.md files that matched' },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['ready', 'hasObjective', 'hasScopeInOut', 'hasDeliverables', 'hasAcceptance', 'workItems', 'issues', 'summary'],
  additionalProperties: false,
  properties: {
    ready: { type: 'boolean', description: 'True only if Objective + Scope(in/out) + Deliverables + Acceptance/verification are present AND every work-item has a source ref + target module + an actionable note' },
    hasObjective: { type: 'boolean' },
    hasScopeInOut: { type: 'boolean', description: 'Has an explicit in/out Scope section' },
    hasDeliverables: { type: 'boolean' },
    hasAcceptance: { type: 'boolean', description: 'Has an Acceptance / verification section (the Definition-of-Done analog)' },
    hasRisks: { type: 'boolean' },
    workItems: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'hasSourceRef', 'hasTargetModule', 'hasActionableNote'],
        additionalProperties: false,
        properties: {
          id: { type: 'string', description: 'Work-item id, e.g. "M0 #6" or the row label' },
          title: { type: 'string' },
          hasSourceRef: { type: 'boolean', description: 'Names a concrete TS source path to port (under /opt/source/specforge/packages/) OR is explicitly marked new/designed' },
          hasTargetModule: { type: 'boolean', description: 'Names the target Python module path' },
          hasActionableNote: { type: 'boolean', description: 'Carries a concrete gotcha / how, not just a restated goal' },
          issues: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    issues: { type: 'array', items: { type: 'string' }, description: 'Milestone-level gaps' },
    summary: { type: 'string' },
  },
}

const AUDIT_SCHEMA = {
  type: 'object',
  required: ['ready', 'additionalIssues', 'verdict'],
  additionalProperties: false,
  properties: {
    ready: { type: 'boolean', description: 'Final readiness after adversarial re-check' },
    additionalIssues: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string' },
  },
}

// --- 1. Resolve ---------------------------------------------------------------
phase('Resolve')
const resolved = await agent(
  `Locate the SpecSmither milestone markdown file for "${MILESTONE}" under ${DIR}/.
Use Glob with pattern "${DIR}/*.md" and pick the single best match for the id, which may be a number prefix ("00","01","02","03"), an "M" id ("M0"), or a slug ("foundation","planning","cli","tui","work","assay").
Mapping hints: "00"/"M0"/"foundation" → 00-foundation.md; "01"/"planning" → 01-planning-lifecycle.md; "02"/"cli"/"tui" → 02-cli-tui.md; "03"/"work" → 03-work-lifecycle.md; "assay" → assay-port.md. A release like "0.1.0" spans 00/01/02 (ambiguous — ask for a specific id); "0.2.0" → 03-work-lifecycle.md. Never match README.md.
Return the absolute path of the single best match in "path", every match in "candidates", and found=false if nothing matches.`,
  { label: `resolve:${MILESTONE}`, phase: 'Resolve', schema: RESOLVE_SCHEMA },
)

if (!resolved || !resolved.found || !resolved.path) {
  return {
    milestone: MILESTONE,
    ready: false,
    error: `Milestone "${MILESTONE}" not found under ${DIR}/.`,
    candidates: resolved && resolved.candidates,
  }
}
log(`Verifying ${resolved.path}`)

// --- 2. Verify ----------------------------------------------------------------
phase('Verify')
const verdict = await agent(
  `Read the milestone document at ${resolved.path} in full, then read ${INVARIANTS} for the SpecSmither cross-cutting invariants and reference corrections.

A SpecSmither milestone must satisfy a fixed structure. Assess it and report every gap:
1. OBJECTIVE — a clear statement of what the milestone delivers.
2. SCOPE — an explicit in/out scope.
3. WORK ITEMS — the discrete units of work (the rows of the work-breakdown table(s); columns are typically "# | Work item | Source to port (TS) | Target module | Notes/gotchas | Status", or an Area→TS→Python-target table). List each one. For EACH:
   - hasSourceRef: it names a concrete TS source path to port under /opt/source/specforge/packages/ (or is explicitly marked "new"/designed-from-schema);
   - hasTargetModule: it names the target Python module path (e.g. dag/cascade.py);
   - hasActionableNote: it carries a concrete gotcha / how-to, not merely a restated goal.
4. DELIVERABLES and ACCEPTANCE/VERIFICATION — the milestone must state what it produces and how it is verified end-to-end (this is the Definition-of-Done analog). RISKS is expected but not blocking.

Flag any work-item missing a source ref, target module, or actionable note (set the boolean false with an explanatory issue). Also flag any work-item that CONTRADICTS an invariant in ${INVARIANTS} (e.g. introduces web/review/canary/JSON-RPC-transport/authoritative-FTS5, applies count deltas instead of in-txn recompute-from-children, or ignores the stated determinism rules).
Set ready=true ONLY if Objective + Scope(in/out) + Deliverables + Acceptance are present AND every work-item has a source ref + target module + an actionable note.
Do not modify the file. Return the structured report.`,
  { label: `verify:${MILESTONE}`, phase: 'Verify', schema: VERIFY_SCHEMA },
)

// --- 3. Audit panel (3 independent adversarial reviewers, distinct lenses) ----
// Single-pass LLM audit is non-deterministic: it surfaces a different slice of
// the long tail each run, so a one-shot ready flag is unstable. A 3-reviewer
// panel with MAJORITY vote turns the gate stable — one over-zealous nitpicker
// can't sink a sound milestone, and genuine blocking gaps that >=2 reviewers see
// still fail it. Each reviewer gets a distinct lens to widen coverage.
phase('Audit')
const LENSES = [
  'CORRECTNESS & FIDELITY — a work-item that contradicts the locked invariants/decisions in planner/README.md (no web/review/canary/JSON-RPC transport; in-txn recompute FROM children not deltas; SQLAlchemy ORM with explicit replace-all; determinism: Math.round half-up, lexLess tie-break, 1-vs-60 estimate default; English prose; no authoritative FTS5; no validator-cache TTL), or that contradicts another work-item or the approved architecture.',
  'COMPLETENESS & COVERAGE — work the milestone requires but no work-item owns: a schema table with no repo, a ported subsystem with no test, a cross-cutting concern (locks, FK cascade, recompute worklist wiring, error mapping) left implicit, an Acceptance/Deliverable bullet with no owning work-item, or a reference-correction (planner/README.md) the milestone silently ignores.',
  'TESTABILITY & SOURCE-TRACE — "acceptance criteria" that are restated goals not runnable assertions; vague notes; cross-item dependencies with no checkpoint; AND verify that the TS "source to port" paths the work-items cite actually EXIST under /opt/source/specforge/packages/ (Glob/Read to confirm) and that target Python module paths are coherent with the plan.',
]
const audits = (
  await parallel(
    LENSES.map((lens, i) => () =>
      agent(
        `You are independent adversarial reviewer #${i + 1} of ${LENSES.length}. Re-read ${resolved.path} in full (and ${INVARIANTS} for the invariants) and scrutinise this first-pass verdict:
${JSON.stringify(verdict, null, 2)}

YOUR LENS: ${lens}

Through that lens, hunt for what the first pass let slide. Where the milestone cites a TS source path, verify it exists in the codebase (Glob/Read under /opt/source/specforge/packages/). Report ONLY genuinely blocking or materially-important new problems in additionalIssues (empty if none — do NOT pad with stylistic nits). Set ready=false only if a real blocking gap remains; otherwise ready=true.`,
        { label: `audit#${i + 1}:${MILESTONE}`, phase: 'Audit', schema: AUDIT_SCHEMA },
      ),
    ),
  )
).filter(Boolean)

const readyVotes = audits.filter((a) => a.ready).length
const auditReady = audits.length ? readyVotes > audits.length / 2 : true // majority
const ready = Boolean(verdict.ready && auditReady)
const consensus = `${readyVotes}/${audits.length} reviewers say ready`
log(ready ? `${MILESTONE}: READY to implement (${consensus})` : `${MILESTONE}: NOT ready (${consensus}) — see issues`)

return {
  milestone: MILESTONE,
  path: resolved.path,
  ready,
  auditConsensus: consensus,
  hasObjective: verdict.hasObjective,
  hasScopeInOut: verdict.hasScopeInOut,
  hasDeliverables: verdict.hasDeliverables,
  hasAcceptance: verdict.hasAcceptance,
  workItems: verdict.workItems,
  issues: [...(verdict.issues || []), ...audits.flatMap((a) => a.additionalIssues || [])],
  summary: verdict.summary,
  auditVerdicts: audits.map((a) => ({ ready: a.ready, verdict: a.verdict })),
}
