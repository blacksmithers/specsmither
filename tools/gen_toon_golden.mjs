// Generate the TOON encoder parity golden from the genuine TS @toon-format/toon
// encoder (v2.1.0).
//
//   cd /opt/source/specsmither && node tools/gen_toon_golden.mjs
//
// Writes fixtures/golden/toon/<case>.json = { input, expected, options? } where
// `expected` is the real TS `encode(input, options)` output. The `input` is
// serialized via JSON.stringify, which is the canonical JS->JSON bridge: the
// Python test re-reads exactly the JS-normalized value (so number formatting,
// integer-key ordering, and -0 collapse are all pinned by the file itself).
//
// CI runs no Node — these fixtures are committed and consumed by the native
// pytest parity gate (tests/test_toon_golden.py).
//
// The hand-authored cases cover TOON's full encode surface:
//   * scalars (string/int/float/negative/bool/null/empty-string) + JS number
//     formatting edge cases (0.000001 vs 1e-7, 1e+21, 1e20 expansion)
//   * nested objects, empty {} / [], deep nesting
//   * arrays of uniform objects -> tabular ([N]{fields}: + rows)
//   * tabular field/value quoting (colon dates, comma-in-description)
//   * ragged / mixed-key / non-primitive arrays -> list items (- ...)
//   * arrays of primitives (inline) and arrays of arrays (list of inline)
//   * objects nested in list items (primitive / array / tabular first value)
//   * every string-quoting trigger (delimiter, newline, quote, backslash, tab,
//     colon, brackets, braces, leading hyphen, leading/trailing ws, numeric-like,
//     leading zero, bool/null-like) + unicode
//   * tab / pipe delimiters, indent=4, and safe key folding (+ flattenDepth)
import { writeFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { encode } from "/opt/source/specforge/node_modules/@toon-format/toon/dist/index.mjs";

// Map a Python-style options dict (as stored in the fixture) to TS encode opts.
function toTsOptions(opts) {
  if (!opts) return undefined;
  const ts = {};
  if (opts.indent !== undefined) ts.indent = opts.indent;
  if (opts.delimiter !== undefined) ts.delimiter = opts.delimiter;
  if (opts.key_folding !== undefined) ts.keyFolding = opts.key_folding;
  if (opts.flatten_depth !== undefined) ts.flattenDepth = opts.flatten_depth;
  return ts;
}

// ---------------------------------------------------------------------------
// Hand-authored cases: { name, value, options? }. `options` is Python-kwarg
// shaped (indent / delimiter / key_folding / flatten_depth).
// ---------------------------------------------------------------------------

const CASES = [
  // --- scalars / root primitives ---
  { name: "scalar_string", value: "hello" },
  { name: "scalar_int", value: 42 },
  { name: "scalar_negative_int", value: -7 },
  { name: "scalar_float", value: 3.14 },
  { name: "scalar_negative_float", value: -2.5 },
  { name: "scalar_float_micro", value: 0.000001 }, // -> "0.000001" (n = -5)
  { name: "scalar_float_tiny", value: 0.0000001 }, // -> "1e-7" (n = -6)
  { name: "scalar_float_large_exp", value: 1e21 }, // -> "1e+21"
  { name: "scalar_float_large_plain", value: 1e20 }, // -> "100000000000000000000"
  { name: "scalar_zero", value: 0 },
  { name: "scalar_bool_true", value: true },
  { name: "scalar_bool_false", value: false },
  { name: "scalar_null", value: null },
  { name: "scalar_empty_string", value: "" }, // -> '""'

  // --- objects ---
  { name: "object_flat", value: { name: "Ada", role: "dev", age: 30 } },
  {
    name: "object_nested",
    value: { context: { task: "Our favorite hikes together", location: "Boulder", season: "spring_2025" } },
  },
  { name: "object_empty", value: {} }, // -> "" (empty string)
  { name: "object_empty_value", value: { meta: {}, name: "x" } },
  { name: "object_deep_nesting", value: { a: { b: { c: { d: { e: 1 } } } } } },
  { name: "object_mixed_value_types", value: { s: "txt", n: 12, f: 1.5, b: true, z: null } },

  // --- arrays of primitives / arrays ---
  { name: "array_empty", value: [] }, // -> "[0]:"
  { name: "array_primitives_ints", value: [1, 2, 3] },
  { name: "array_primitives_strings", value: ["ana", "luis", "sam"] },
  { name: "array_primitives_mixed", value: [1, "two", true, null, 3.5] },
  { name: "array_of_arrays", value: [[1, 2], [3, 4], [5, 6]] },
  { name: "array_of_arrays_ragged", value: [[1, 2, 3], [4], []] },
  { name: "object_with_inline_array", value: { friends: ["ana", "luis", "sam"], count: 3 } },

  // --- tabular (uniform array of objects) ---
  {
    name: "tabular_uniform",
    value: { users: [
      { id: 1, name: "Alice", role: "admin" },
      { id: 2, name: "Bob", role: "user" },
    ] },
  },
  {
    name: "tabular_root_array",
    value: [
      { id: 1, name: "Alice", role: "admin" },
      { id: 2, name: "Bob", role: "user" },
    ],
  },
  {
    name: "tabular_with_quoting",
    value: { repositories: [
      { id: 28457823, name: "freeCodeCamp", repo: "freeCodeCamp/freeCodeCamp",
        description: "freeCodeCamp.org's open-source codebase, learn to code",
        createdAt: "2014-12-24T17:49:19Z", stars: 430886, defaultBranch: "main" },
      { id: 21737465, name: "awesome", repo: "sindresorhus/awesome",
        description: "😎 Awesome lists about all kinds of interesting topics",
        createdAt: "2014-07-11T13:42:37Z", stars: 410052, defaultBranch: "main" },
    ] },
  },
  {
    name: "tabular_full_hikes",
    value: {
      context: { task: "Our favorite hikes together", location: "Boulder", season: "spring_2025" },
      friends: ["ana", "luis", "sam"],
      hikes: [
        { id: 1, name: "Blue Lake Trail", distanceKm: 7.5, elevationGain: 320, companion: "ana", wasSunny: true },
        { id: 2, name: "Ridge Overlook", distanceKm: 9.2, elevationGain: 540, companion: "luis", wasSunny: false },
        { id: 3, name: "Wildflower Loop", distanceKm: 5.1, elevationGain: 180, companion: "sam", wasSunny: true },
      ],
    },
  },

  // --- ragged / mixed-key / non-primitive arrays -> list items ---
  { name: "list_ragged_key_count", value: [{ a: 1 }, { a: 1, b: 2 }] },
  { name: "list_mixed_keys_same_count", value: [{ id: 1, name: "x" }, { id: 2, title: "y" }] },
  {
    name: "list_object_nonprimitive_value",
    value: [{ id: 1, tags: ["a", "b"] }, { id: 2, tags: ["c"] }],
  },
  { name: "list_empty_object_member", value: [{}, { a: 1 }] },
  {
    name: "list_item_first_value_tabular",
    value: [{ name: "group", members: [{ k: 1 }, { k: 2 }], note: "done" }],
  },
  {
    name: "list_item_first_value_primitive_array",
    value: [{ name: "x", nums: [1, 2, 3] }],
  },
  {
    name: "list_item_first_value_empty_array",
    value: [{ items: [], name: "x" }],
  },
  {
    name: "list_item_nested_object",
    value: [{ name: "x", child: { deep: 1 } }],
  },

  // --- string quoting / escaping triggers (default comma delimiter) ---
  { name: "string_with_comma", value: { note: "a,b,c" } },
  { name: "string_with_colon", value: { time: "12:30" } },
  { name: "string_with_double_quote", value: { q: 'say "hi" now' } },
  { name: "string_with_backslash", value: { p: "a\\b" } },
  { name: "string_with_newline", value: { text: "line1\nline2" } },
  { name: "string_with_tab", value: { text: "a\tb" } },
  { name: "string_with_carriage_return", value: { text: "a\rb" } },
  { name: "string_with_open_bracket", value: { s: "[x]" } },
  { name: "string_with_brace", value: { s: "{x}" } },
  { name: "string_leading_hyphen", value: { s: "-dash" } },
  { name: "string_leading_trailing_ws", value: { s: " pad " } },
  { name: "string_numeric_like", value: { s: "123" } },
  { name: "string_float_like", value: { s: "-3.14" } },
  { name: "string_exp_like", value: { s: "1e-6" } },
  { name: "string_leading_zero", value: { s: "007" } },
  { name: "string_bool_like", value: { s: "true" } },
  { name: "string_null_like", value: { s: "null" } },
  { name: "string_pipe_ok_under_comma", value: { s: "a|b" } }, // pipe is NOT the delimiter -> unquoted
  { name: "string_unicode", value: { emoji: "😎 héllo wörld 日本語" } },
  { name: "key_needing_quotes", value: { "weird key!": 1, "a.b": 2, ok: 3 } },

  // --- delimiter / indent options ---
  {
    name: "tabular_tab_delimiter",
    options: { delimiter: "\t" },
    value: { users: [{ id: 1, name: "Alice", role: "admin" }, { id: 2, name: "Bob", role: "user" }] },
  },
  {
    name: "tabular_pipe_delimiter",
    options: { delimiter: "|" },
    value: { users: [{ id: 1, name: "Alice", role: "admin" }, { id: 2, name: "Bob", role: "user" }] },
  },
  {
    name: "inline_array_tab_delimiter",
    options: { delimiter: "\t" },
    value: { nums: [1, 2, 3] },
  },
  {
    name: "string_with_pipe_under_pipe_delim",
    options: { delimiter: "|" },
    value: { s: "a|b" }, // pipe IS the delimiter here -> quoted
  },
  {
    name: "indent_four_nested",
    options: { indent: 4 },
    value: { a: { b: { c: 1 } }, list: [{ id: 1, v: 2 }, { id: 2, v: 3 }] },
  },

  // --- safe key folding ---
  {
    name: "keyfolding_safe_chain",
    options: { key_folding: "safe" },
    value: { a: { b: { c: 1 } } }, // -> "a.b.c: 1"
  },
  {
    name: "keyfolding_safe_partial",
    options: { key_folding: "safe" },
    value: { wrapper: { inner: { x: 1, y: 2 } } }, // -> "wrapper.inner:" then x/y
  },
  {
    name: "keyfolding_flatten_depth",
    options: { key_folding: "safe", flatten_depth: 2 },
    value: { a: { b: { c: { d: 1 } } } }, // folds only 2 segments: "a.b:" then "c:" then "d: 1"
  },
  {
    name: "keyfolding_off_default",
    value: { a: { b: { c: 1 } } }, // off -> nested lines
  },
];

const dir = resolve(import.meta.dirname, "../fixtures/golden/toon");
mkdirSync(dir, { recursive: true });

let ok = 0;
for (const c of CASES) {
  const expected = encode(c.value, toTsOptions(c.options));
  const fixture = c.options
    ? { input: c.value, options: c.options, expected }
    : { input: c.value, expected };
  writeFileSync(`${dir}/${c.name}.json`, JSON.stringify(fixture, null, 2) + "\n");
  ok++;
}
console.log(`wrote TOON golden for ${ok} cases -> ${dir}`);
