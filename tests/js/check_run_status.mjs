/**
 * Exercise the real run-status rendering out of pipeline/static/app.js.
 *
 * The masthead used to be able to say only "running" or "idle", and said
 * "idle" for every run the dashboard did not start itself. These checks pin
 * the line that replaced it: who is running, since when, inside which
 * recording, and how much queue is left.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync("pipeline/static/app.js", "utf8");

const noop = () => {};

function fakeElement(id) {
  const classes = new Set();
  return {
    id,
    innerHTML: "",
    textContent: "",
    hidden: false,
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
    },
  };
}

const elements = new Map([["pipeline-detail", fakeElement("pipeline-detail")]]);

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

const exposed = `${source}
globalThis.__exports = { renderPipelineDetail, clockOf, QUEUE_LABELS, STAGE_FRIENDLY };`;
new vm.Script(exposed).runInContext(sandbox);

const { renderPipelineDetail, clockOf, QUEUE_LABELS, STAGE_FRIENDLY } = sandbox.__exports;
const detail = elements.get("pipeline-detail");

// ── Active polling reads only the lightweight status endpoint ─────────
// /api/meetings reads every compiled minutes file. Loading it (plus overview)
// every 1.5 seconds while a run was active kept the dashboard near one CPU
// core even though the pipeline worker itself was correctly asynchronous.
const activePoll = source.match(
  /if \(status\.running && !state\.pollTimer\) \{[\s\S]*?\}, 1500\);/
);
assert.ok(activePoll, "active status poll was not found");
assert.ok(activePoll[0].includes("checkPipelineStatus()"));
assert.ok(!activePoll[0].includes("loadOverview()"));
assert.ok(!activePoll[0].includes("loadMeetings()"));

// The archive still refreshes exactly when the run reaches a terminal state.
const terminalRefresh = source.match(
  /if \(wasRunning && !status\.running\) \{[\s\S]*?loadCommitments\(\);\s*\}/
);
assert.ok(terminalRefresh, "terminal refresh block was not found");
assert.ok(terminalRefresh[0].includes("loadOverview()"));
assert.ok(terminalRefresh[0].includes("loadMeetings()"));

// ── A run started outside the dashboard says so, and says since when ──
renderPipelineDetail({
  running: true,
  owner: "external",
  holder_pid: 33068,
  started_at: "2026-09-02T14:00:09-04:00",
  stage: "transcribe",
  in_flight: { stage: "transcribe", label: "2026-09-02 standup" },
  queue: { discovered: 3, speakers_resolved: 12, failed: 2 },
});
assert.equal(detail.hidden, false);
assert.ok(detail.innerHTML.includes("started outside the dashboard"));
assert.ok(detail.innerHTML.includes("pid 33068"));
assert.ok(detail.innerHTML.includes("Transcribing Speech"));
assert.ok(detail.innerHTML.includes("2026-09-02 standup"));
// The queue reads as work owed, not as database statuses.
assert.ok(detail.innerHTML.includes("3 to transcribe"));
assert.ok(detail.innerHTML.includes("12 to compile"));
assert.ok(detail.innerHTML.includes("2 failed"));
assert.ok(detail.innerHTML.includes("pd-chip-failed"));
assert.equal(detail.classList.contains("pd-external"), true);

// ── The dashboard's own run does not accuse itself of being foreign ───
renderPipelineDetail({
  running: true,
  owner: "dashboard",
  started_at: "2026-09-02T15:10:00-04:00",
  stage: "minutes",
  in_flight: { stage: "minutes", label: "2026-09-02 review" },
  queue: { speakers_resolved: 4 },
});
assert.ok(!detail.innerHTML.includes("started outside the dashboard"));
assert.ok(detail.innerHTML.includes("Synthesizing Minutes"));
assert.equal(detail.classList.contains("pd-external"), false);

// ── A refused click reports the run that owns the queue ───────────────
renderPipelineDetail({
  running: false,
  owner: null,
  blocked_by: { pid: 33068, started_at: "2026-09-02T14:00:09-04:00" },
  queue: {},
});
assert.ok(detail.innerHTML.includes("owns the queue"));
assert.ok(!detail.innerHTML.toLowerCase().includes("crash"));

// ── Idle with nothing queued renders nothing at all ───────────────────
renderPipelineDetail({ running: false, owner: null, queue: {} });
assert.equal(detail.hidden, true);
assert.equal(detail.innerHTML, "");

// ── A zero count is not worth a chip ──────────────────────────────────
renderPipelineDetail({ running: false, owner: null, queue: { failed: 0, discovered: 2 } });
assert.ok(detail.innerHTML.includes("2 to transcribe"));
assert.ok(!detail.innerHTML.includes("failed"));

// ── Timestamps ────────────────────────────────────────────────────────
assert.equal(clockOf(null), "");
assert.equal(clockOf("not a date"), "not a date");
assert.match(clockOf("2026-09-02T14:00:09-04:00"), /\d{1,2}:\d{2}/);

// ── Vocabulary covers every status and stage the API can send ─────────
for (const status of [
  "discovered", "transcribed", "speakers_resolved", "minutes_compiled", "indexed", "failed",
]) {
  assert.ok(QUEUE_LABELS[status], `no label for status ${status}`);
}
for (const stage of [
  "all", "capture", "ingest", "transcribe", "speakers", "minutes", "index",
  "graph-sync", "recompile",
]) {
  assert.ok(STAGE_FRIENDLY[stage], `no friendly name for stage ${stage}`);
}

console.log("run status checks passed");
