// Generate the DAG parity golden from the CURRENT TS SpecForge tree aggregators.
//
//   SPECFORGE=/opt/source/specforge \
//   TSX_TSCONFIG_PATH=$SPECFORGE/tsconfig.base.json \
//   npx tsx tools/gen_dag_golden.mjs
//
// Writes fixtures/golden/dag/<case>.json = { kind, input, expected } where
// `expected` is the genuine TS function's output, normalized to the exact shape
// the Python port emits and with the volatile tree fields (generatedAt, version)
// stripped. CI runs no Node — these fixtures are committed and consumed by the
// native pytest parity gate (tests/test_dag_golden.py).
//
// The hand-authored cases deliberately cover the determinism traps:
//   critical_path_lexless         (a) lexLess tie-break: two equal-minute paths
//   critical_path_zero_weight     (b') 0 / undefined estimate weighs 1 in path SELECTION
//   build_tree_undefined_zero     (b) 0 -> displays 0, undefined -> displays 60 (?? 60)
//   build_tree_halfup             (c) Math.round half-up of a fractional minute sum
//   find_cycles_scc_selfloop      (d) 2-node SCC + self-loop (+ a 3-cycle), sorted
//   blocker_depth_done_terminator (e) a 'done' dep is dropped and terminates the chain
//   build_tree_basic              (f) a full small spec (ready/blocked + ticketId order)
import { writeFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

const SPECFORGE = process.env.SPECFORGE ?? "/opt/source/specforge";
const TREE = `${SPECFORGE}/packages/operations/src/aggregators/tree`;

const { computeCriticalPath } = await import(`${TREE}/critical-path.ts`);
const { computeMaxBlockerDepth } = await import(`${TREE}/blocker-depth.ts`);
const { findCycles } = await import(`${TREE}/cycle-detection.ts`);
const { buildDependencyTree } = await import(`${TREE}/build-dependency-tree.ts`);

// A pinned clock so generatedAt is deterministic before it is stripped.
const FIXED_NOW = () => new Date("2026-06-26T00:00:00.000Z");
const NO_WARN = () => {};

// ---------------------------------------------------------------------------
// Normalizers: turn each genuine TS output into the exact shape the Python port
// produces (camelCase preserved; epic.status / version / generatedAt dropped —
// the Python pure-DAG tree intentionally omits epic status, recon A5 §1).
// ---------------------------------------------------------------------------

function serializeCpNode(n) {
  return { id: n.id, ticketNumber: n.ticketNumber, title: n.title, estimatedMinutes: n.estimatedMinutes };
}

function serializeTicket(v) {
  const o = {
    id: v.id,
    ticketNumber: v.ticketNumber,
    title: v.title,
    status: v.status,
    epicId: v.epicId,
    epicNumber: v.epicNumber,
    dependencies: v.dependencies,
    blocks: v.blocks,
    isReady: v.isReady,
    blockerCount: v.blockerCount,
    unsatisfiedDeps: v.unsatisfiedDeps,
  };
  // TreeTicket.estimatedMinutes is optional: undefined is omitted by JSON.stringify
  // (matches the Python serializer, which omits None). A literal 0 is kept.
  if (v.estimatedMinutes !== undefined) o.estimatedMinutes = v.estimatedMinutes;
  return o;
}

function serializeSummary(s) {
  return {
    totalTickets: s.totalTickets,
    ticketsByStatus: {
      pending: s.ticketsByStatus.pending,
      ready: s.ticketsByStatus.ready,
      active: s.ticketsByStatus.active,
      done: s.ticketsByStatus.done,
    },
    readyTickets: s.readyTickets,
    blockedTickets: s.blockedTickets,
    hasCircularDeps: s.hasCircularDeps,
    criticalPathLength: s.criticalPathLength,
    criticalPathMinutes: s.criticalPathMinutes,
    maxBlockerDepth: s.maxBlockerDepth,
    criticalPath: s.criticalPath.map(serializeCpNode),
    estimatedMinutesTotal: s.estimatedMinutesTotal,
  };
}

function serializeTree(t) {
  return {
    specificationId: t.specificationId,
    epics: t.epics.map((e) => ({
      id: e.id,
      epicNumber: e.epicNumber,
      title: e.title,
      ticketIds: e.ticketIds,
    })),
    tickets: Object.fromEntries(Object.entries(t.tickets).map(([k, v]) => [k, serializeTicket(v)])),
    summary: serializeSummary(t.summary),
    criticalPath: t.criticalPath.map(serializeCpNode),
    edges: t.edges.map((e) => ({ from: e.from, to: e.to })),
    cycles: t.cycles.map((c) => ({ cycle: c.cycle })),
  };
}

// ---------------------------------------------------------------------------
// Per-kind runners: adapt the canonical fixture `input` to each TS function's
// real input shape, run it, and normalize the output to `expected`.
// ---------------------------------------------------------------------------

function runCriticalPath(input) {
  const r = computeCriticalPath({
    nodes: input.nodes.map((n) => ({ id: n.id, estimatedMinutes: n.estimatedMinutes ?? 0 })),
    edges: input.edges,
    excludedIds: new Set(input.excludedIds ?? []),
  });
  return { path: r.path, minutes: r.minutes };
}

function runBlockerDepth(input) {
  const r = computeMaxBlockerDepth({
    nodes: input.nodes.map((n) => ({ id: n.id, isComplete: n.status === "done" })),
    edges: input.edges,
    excludedIds: new Set(input.excludedIds ?? []),
  });
  return { maxDepth: r.maxDepth };
}

function runFindCycles(input) {
  return findCycles(input.nodeIds, input.edges).map((x) => x.cycle);
}

function runBuildTree(input) {
  const tree = buildDependencyTree(
    {
      specificationId: input.specificationId,
      previousVersion: input.previousVersion,
      tickets: input.tickets,
      epics: input.epics,
      dependencies: input.dependencies,
    },
    FIXED_NOW,
    NO_WARN,
  );
  return serializeTree(tree);
}

const RUNNERS = {
  critical_path: runCriticalPath,
  blocker_depth: runBlockerDepth,
  find_cycles: runFindCycles,
  build_tree: runBuildTree,
};

// ---------------------------------------------------------------------------
// Hand-authored cases.
// ---------------------------------------------------------------------------

const CASES = [
  // (a) lexLess tie-break: n3 depends on n1 and n2 with equal weights; the
  //     equal-minute paths [n1] vs [n2] tie inside longestTo(n3) and the
  //     lexicographically smaller (n1) wins => path [n1, n3].
  {
    name: "critical_path_lexless",
    kind: "critical_path",
    input: {
      nodes: [
        { id: "n1", estimatedMinutes: 10 },
        { id: "n2", estimatedMinutes: 10 },
        { id: "n3", estimatedMinutes: 10 },
      ],
      edges: [
        { ticketId: "n3", dependsOnId: "n1" },
        { ticketId: "n3", dependsOnId: "n2" },
      ],
    },
  },

  // (b') 0 -> 1 and undefined -> 1 weight in path SELECTION. Chain c -> a -> b
  //      with a.est=0 and b.est=undefined; total weight = 1 + 1 + 5 = 7.
  {
    name: "critical_path_zero_weight",
    kind: "critical_path",
    input: {
      nodes: [
        { id: "a", estimatedMinutes: 0 },
        { id: "b" },
        { id: "c", estimatedMinutes: 5 },
      ],
      edges: [
        { ticketId: "c", dependsOnId: "a" },
        { ticketId: "a", dependsOnId: "b" },
      ],
    },
  },

  // (e) blocker depth: a -> b -> c -> d with d 'done'. The c->d edge is dropped
  //     (done dep does not block) so depth(c)=0, depth(b)=1, depth(a)=2.
  {
    name: "blocker_depth_done_terminator",
    kind: "blocker_depth",
    input: {
      nodes: [
        { id: "a", status: "pending" },
        { id: "b", status: "pending" },
        { id: "c", status: "pending" },
        { id: "d", status: "done" },
      ],
      edges: [
        { ticketId: "a", dependsOnId: "b" },
        { ticketId: "b", dependsOnId: "c" },
        { ticketId: "c", dependsOnId: "d" },
      ],
    },
  },

  // (d) cycles: 2-node SCC {a,b}, a self-loop {s}, a 3-cycle {x,y,z}, and an
  //     isolated node 'iso'. Result sorted by cycle[0]; each cycle[] sorted.
  {
    name: "find_cycles_scc_selfloop",
    kind: "find_cycles",
    input: {
      nodeIds: ["a", "b", "iso", "s", "x", "y", "z"],
      edges: [
        { ticketId: "a", dependsOnId: "b" },
        { ticketId: "b", dependsOnId: "a" },
        { ticketId: "s", dependsOnId: "s" },
        { ticketId: "x", dependsOnId: "y" },
        { ticketId: "y", dependsOnId: "z" },
        { ticketId: "z", dependsOnId: "x" },
      ],
    },
  },

  // (b) display defaults: chain A -> B -> C with B.est=0 (displays 0) and
  //     C.est=undefined (displays 60). criticalPath = [C,B,A] (weights 1,1,10),
  //     criticalPathMinutes=12, estimatedMinutesTotal=10+0+60=70.
  {
    name: "build_tree_undefined_zero",
    kind: "build_tree",
    input: {
      specificationId: "spec-disp",
      previousVersion: 0,
      epics: [{ id: "e1", epicNumber: 1, title: "E1", status: "in_progress" }],
      tickets: [
        { id: "A", ticketNumber: 1, title: "A", status: "pending", epicId: "e1", estimatedMinutes: 10 },
        { id: "B", ticketNumber: 2, title: "B", status: "pending", epicId: "e1", estimatedMinutes: 0 },
        { id: "C", ticketNumber: 3, title: "C", status: "pending", epicId: "e1" },
      ],
      dependencies: [
        { ticketId: "A", dependsOnId: "B" },
        { ticketId: "B", dependsOnId: "C" },
      ],
    },
  },

  // (c) half-up rounding: X depends on Y with X.est=2.5, Y.est=30. The path
  //     [Y,X] sums to 32.5; Math.round half-up => 33 (Python banker's would 32).
  {
    name: "build_tree_halfup",
    kind: "build_tree",
    input: {
      specificationId: "spec-half",
      epics: [{ id: "e1", epicNumber: 1, title: "E1", status: "in_progress" }],
      tickets: [
        { id: "X", ticketNumber: 1, title: "X", status: "pending", epicId: "e1", estimatedMinutes: 2.5 },
        { id: "Y", ticketNumber: 2, title: "Y", status: "pending", epicId: "e1", estimatedMinutes: 30 },
      ],
      dependencies: [{ ticketId: "X", dependsOnId: "Y" }],
    },
  },

  // (f) full small spec: a done root, ready + blocked split across two epics,
  //     ticketNumber-primary ordering of readyTickets / ticketIds.
  {
    name: "build_tree_basic",
    kind: "build_tree",
    input: {
      specificationId: "spec-1",
      previousVersion: 0,
      epics: [
        { id: "e1", epicNumber: 1, title: "Epic One", status: "in_progress" },
        { id: "e2", epicNumber: 2, title: "Epic Two", status: "todo" },
      ],
      tickets: [
        { id: "t1", ticketNumber: 1, title: "T1", status: "done", epicId: "e1", estimatedMinutes: 30 },
        { id: "t2", ticketNumber: 3, title: "T2", status: "ready", epicId: "e1", estimatedMinutes: 60 },
        { id: "t3", ticketNumber: 4, title: "T3", status: "pending", epicId: "e2", estimatedMinutes: 45 },
        { id: "t4", ticketNumber: 2, title: "T4", status: "ready", epicId: "e2", estimatedMinutes: 20 },
        { id: "t5", ticketNumber: 5, title: "T5", status: "pending", epicId: "e2", estimatedMinutes: 15 },
      ],
      dependencies: [
        { ticketId: "t2", dependsOnId: "t1" },
        { ticketId: "t3", dependsOnId: "t2" },
      ],
    },
  },
];

const dir = resolve(import.meta.dirname, "../fixtures/golden/dag");
mkdirSync(dir, { recursive: true });

let ok = 0;
for (const c of CASES) {
  const expected = RUNNERS[c.kind](c.input);
  const fixture = { kind: c.kind, input: c.input, expected };
  writeFileSync(`${dir}/${c.name}.json`, JSON.stringify(fixture, null, 2) + "\n");
  ok++;
}
console.log(`wrote DAG golden for ${ok} cases -> ${dir}`);
