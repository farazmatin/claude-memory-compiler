/**
 * Exercise the real sort/filter logic out of pipeline/static/app.js.
 *
 * There is no JS test runner in this repo, so this drives the shipped source
 * directly: the module is evaluated with the smallest possible DOM stubs and the
 * comparators and predicates are called with the two row shapes the API
 * actually returns.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync("pipeline/static/app.js", "utf8");

const noop = () => {};
const elements = new Map();
const sandbox = {
  console,
  document: {
    addEventListener: noop,
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop }),
  },
  window: { addEventListener: noop, matchMedia: () => ({ matches: false }) },
  localStorage: {
    store: new Map(),
    getItem(k) { return this.store.has(k) ? this.store.get(k) : null; },
    setItem(k, v) { this.store.set(k, String(v)); },
  },
  setInterval: noop,
  setTimeout: noop,
  fetch: () => Promise.reject(new Error("no network in this harness")),
  Date,
  JSON,
  Math,
  Number,
  Boolean,
  String,
  Array,
  Object,
  Set,
  Map,
  Intl,
  isNaN,
  parseInt,
  parseFloat,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
// Top-level `const` stays in the script's lexical scope and never lands on the
// global object, so the sort tables have to be handed out explicitly. Appended
// to the real source rather than copied out of it - this still tests shipped code.
const exposed = `${source}
globalThis.__exports = {
  MEETING_SORTS, SPEAKER_SORTS,
  normalizeSpeakerRow, speakerMatchesFilters, isoDaysAgo, compareBy,
};`;
new vm.Script(exposed).runInContext(sandbox);

const {
  MEETING_SORTS,
  SPEAKER_SORTS,
  normalizeSpeakerRow,
  speakerMatchesFilters,
  isoDaysAgo,
  compareBy,
} = sandbox.__exports;

const order = (rows, comparator) => rows.slice().sort(comparator);
let checks = 0;
const check = (label, fn) => {
  fn();
  checks += 1;
  console.log(`  ok  ${label}`);
};

// ── The two row shapes the API really returns ──────────────────────────
const cluster = {
  id: "c1", size: 4, total_speech: 300, best_canonical: "Yuliya",
  best_score: 0.72, next_canonical: "Ruth", next_score: 0.71, band: "review",
  clip_seconds: 18, llm_suggestion: null,
  members: [{ meeting_date: "2026-03-01" }, { meeting_date: "2026-08-02" }],
};
const oneOff = {
  meeting_id: "m1", label: "SPEAKER_00", speech_sec: 44, snippet_count: 0,
  best_canonical: null, best_score: null, next_canonical: null, next_score: null,
  band: "new", llm_suggestion: "Dan", meeting_date: "2026-07-15",
};

console.log("normalizeSpeakerRow reads both shapes");
check("cluster speech comes from total_speech", () =>
  assert.equal(normalizeSpeakerRow(cluster).speech, 300));
check("one-off speech comes from speech_sec", () =>
  assert.equal(normalizeSpeakerRow(oneOff).speech, 44));
check("cluster occurrences come from size", () =>
  assert.equal(normalizeSpeakerRow(cluster).occurrences, 4));
check("a one-off counts as one occurrence", () =>
  assert.equal(normalizeSpeakerRow(oneOff).occurrences, 1));
check("cluster date is the LATEST member meeting", () =>
  assert.equal(normalizeSpeakerRow(cluster).latestDate, "2026-08-02"));
check("one-off date comes from its own meeting", () =>
  assert.equal(normalizeSpeakerRow(oneOff).latestDate, "2026-07-15"));
check("margin is best minus next", () =>
  assert.equal(normalizeSpeakerRow(cluster).margin, 0.01));
check("no runner-up means no margin, not a margin of zero", () =>
  assert.equal(normalizeSpeakerRow(oneOff).margin, null));
check("clip presence reads clip_seconds for clusters", () =>
  assert.equal(normalizeSpeakerRow(cluster).hasClip, true));
check("clip presence reads snippet_count for one-offs", () =>
  assert.equal(normalizeSpeakerRow(oneOff).hasClip, false));

console.log("SPEAKER_SORTS put the row needing a human first");
const thin = { ...cluster, id: "thin", best_score: 0.72, next_score: 0.71 };
const wide = { ...cluster, id: "wide", best_score: 0.95, next_score: 0.20 };
const unscored = { ...oneOff, label: "unscored" };
check("closest call first ranks a 0.01 margin above a 0.75 margin", () =>
  assert.deepEqual(
    order([wide, thin], SPEAKER_SORTS["margin-asc"]).map((r) => r.id),
    ["thin", "wide"],
  ));
check("an unscored row sorts LAST in ascending margin, not first", () =>
  assert.equal(
    order([unscored, thin, wide], SPEAKER_SORTS["margin-asc"]).at(-1).label,
    "unscored",
  ));
check("an unscored row also sorts last by descending confidence", () =>
  assert.equal(
    order([unscored, thin], SPEAKER_SORTS["confidence-desc"]).at(-1).label,
    "unscored",
  ));
check("least confident first still keeps unscored rows last", () =>
  assert.equal(
    order([unscored, thin, wide], SPEAKER_SORTS["confidence-asc"]).at(-1).label,
    "unscored",
  ));
check("speech sort compares a cluster against a one-off correctly", () =>
  assert.deepEqual(
    order([oneOff, cluster], SPEAKER_SORTS["speech-desc"]).map((r) => r.id || r.label),
    ["c1", "SPEAKER_00"],
  ));
check("occurrences sort puts the 4-meeting cluster above a one-off", () =>
  assert.deepEqual(
    order([oneOff, cluster], SPEAKER_SORTS["occurrences-desc"]).map((r) => r.id || r.label),
    ["c1", "SPEAKER_00"],
  ));
check("date sort mixes both shapes by their real dates", () =>
  assert.deepEqual(
    order([oneOff, cluster], SPEAKER_SORTS["date-desc"]).map((r) => r.id || r.label),
    ["c1", "SPEAKER_00"],
  ));

console.log("speakerMatchesFilters reaches BOTH shapes");
const all = { band: "all", suggestion: "all", clip: "all" };
check("band filter matches a cluster", () =>
  assert.equal(speakerMatchesFilters(cluster, { ...all, band: "review" }), true));
check("band filter excludes a cluster in another band", () =>
  assert.equal(speakerMatchesFilters(cluster, { ...all, band: "new" }), false));
check("band filter genuinely applies to one-offs too", () => {
  assert.equal(speakerMatchesFilters(oneOff, { ...all, band: "new" }), true);
  assert.equal(speakerMatchesFilters(oneOff, { ...all, band: "review" }), false);
});
check("voiceprint filter keeps the matched cluster, drops the unmatched one-off", () => {
  assert.equal(speakerMatchesFilters(cluster, { ...all, suggestion: "voiceprint" }), true);
  assert.equal(speakerMatchesFilters(oneOff, { ...all, suggestion: "voiceprint" }), false);
});
check("transcript filter keeps the row named in the room", () => {
  assert.equal(speakerMatchesFilters(oneOff, { ...all, suggestion: "transcript" }), true);
  assert.equal(speakerMatchesFilters(cluster, { ...all, suggestion: "transcript" }), false);
});
check("'no suggestion at all' needs both signals absent", () => {
  const bare = { ...oneOff, llm_suggestion: null };
  assert.equal(speakerMatchesFilters(bare, { ...all, suggestion: "none" }), true);
  assert.equal(speakerMatchesFilters(oneOff, { ...all, suggestion: "none" }), false);
  assert.equal(speakerMatchesFilters(cluster, { ...all, suggestion: "none" }), false);
});
check("clip filter separates playable from silent rows", () => {
  assert.equal(speakerMatchesFilters(cluster, { ...all, clip: "yes" }), true);
  assert.equal(speakerMatchesFilters(oneOff, { ...all, clip: "yes" }), false);
  assert.equal(speakerMatchesFilters(oneOff, { ...all, clip: "no" }), true);
});
check("filters compose rather than overriding one another", () =>
  assert.equal(
    speakerMatchesFilters(cluster, { band: "review", suggestion: "voiceprint", clip: "yes" }),
    true,
  ));

console.log("MEETING_SORTS");
const older = { id: "old", date: "2026-01-05", time: "09:00", duration_sec: 600, speaker_count: 2, unresolved_count: 0, title: "Zebra" };
const newer = { id: "new", date: "2026-08-20", time: "14:00", duration_sec: 9000, speaker_count: 7, unresolved_count: 5, title: "Alpha" };
const undated = { id: "undated", date: null, time: null, duration_sec: null, speaker_count: 0, unresolved_count: 0, title: "Broken" };
check("newest first", () =>
  assert.deepEqual(order([older, newer], MEETING_SORTS["date-desc"]).map((m) => m.id), ["new", "old"]));
check("oldest first", () =>
  assert.deepEqual(order([newer, older], MEETING_SORTS["date-asc"]).map((m) => m.id), ["old", "new"]));
check("an undated meeting sorts last in BOTH directions, never to the top", () => {
  assert.equal(order([undated, older, newer], MEETING_SORTS["date-desc"]).at(-1).id, "undated");
  assert.equal(order([undated, older, newer], MEETING_SORTS["date-asc"]).at(-1).id, "undated");
});
check("longest first", () =>
  assert.deepEqual(order([older, newer], MEETING_SORTS["duration-desc"]).map((m) => m.id), ["new", "old"]));
check("most speakers first", () =>
  assert.deepEqual(order([older, newer], MEETING_SORTS["speakers-desc"]).map((m) => m.id), ["new", "old"]));
check("most unnamed speakers first", () =>
  assert.deepEqual(order([older, newer], MEETING_SORTS["unresolved-desc"]).map((m) => m.id), ["new", "old"]));
check("title A-Z", () =>
  assert.deepEqual(order([older, newer], MEETING_SORTS["title-asc"]).map((m) => m.id), ["new", "old"]));
check("same-day meetings break the tie on time", () => {
  const morning = { id: "am", date: "2026-08-20", time: "09:00" };
  const evening = { id: "pm", date: "2026-08-20", time: "18:00" };
  assert.deepEqual(order([morning, evening], MEETING_SORTS["date-desc"]).map((m) => m.id), ["pm", "am"]);
});

console.log("date window");
check("isoDaysAgo returns a plain YYYY-MM-DD cutoff in the past", () => {
  const cutoff = isoDaysAgo(30);
  assert.match(cutoff, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(cutoff < new Date().toISOString().slice(0, 10));
});
check("a 30-day window keeps a recent date and drops an old one", () => {
  const cutoff = isoDaysAgo(30);
  assert.ok(new Date().toISOString().slice(0, 10) >= cutoff);
  assert.ok("2020-01-01" < cutoff);
});

console.log("comparators are stable and side-effect free");
check("sorting does not mutate the input array", () => {
  const rows = [newer, older, undated];
  const before = rows.map((r) => r.id);
  order(rows, MEETING_SORTS["date-desc"]);
  assert.deepEqual(rows.map((r) => r.id), before);
});
check("compareBy reports equality as 0 so equal rows keep their order", () =>
  assert.equal(compareBy((r) => r.v, "desc")({ v: 1 }, { v: 1 }), 0));

console.log(`\n${checks} checks passed`);
