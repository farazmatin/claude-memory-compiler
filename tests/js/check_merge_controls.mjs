/** Exercise the real people-merge controls from pipeline/static/app.js. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync("pipeline/static/app.js", "utf8");
const noop = () => {};
const elements = new Map();

function element(overrides = {}) {
  return {
    value: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    disabled: false,
    dataset: {},
    style: {},
    classList: { add: noop, remove: noop, toggle: noop },
    addEventListener: noop,
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    focus() { this.focused = true; },
    select: noop,
    showModal() { this.open = true; },
    close() { this.open = false; },
    closest: () => null,
    appendChild: noop,
    remove: noop,
    ...overrides,
  };
}

for (const id of [
  "people-suggestions",
  "people-suggestion-yes",
  "people-suggestion-rename",
  "people-suggestion-no",
  "people-suggestion-target",
  "people-suggestion-rename-panel",
  "people-suggestion-review",
  "people-suggestion-preview-panel",
  "people-suggestion-preview",
  "people-suggestion-confirm",
  "person-merge-modal",
  "person-merge-sources",
  "person-merge-selected-list",
  "person-merge-target",
  "person-merge-target-custom",
  "person-merge-preview",
  "person-merge-save",
  "person-rename-modal",
  "person-rename-source",
  "person-rename-name",
  "person-rename-preview",
  "person-rename-save",
  "toast-container",
]) elements.set(id, element());

const sandbox = {
  console,
  document: {
    addEventListener: noop,
    getElementById: (id) => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => element(),
  },
  window: { addEventListener: noop },
  localStorage: { getItem: () => null, setItem: noop },
  crypto: { randomUUID: () => "00000000-0000-0000-0000-000000000000" },
  setInterval: noop,
  setTimeout: (fn) => fn(),
  fetch: () => Promise.reject(new Error("unexpected network call")),
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
  Promise,
  isNaN,
  parseInt,
  parseFloat,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
new vm.Script(`${source}\nrefreshPeopleDependentViews = () => {};\nglobalThis.__mergeExports = {
  state,
  revealPeopleSuggestionRename,
  previewPeopleSuggestion,
  confirmPeopleSuggestionMerge,
  openSelectedPeopleMergeModal,
  submitMergePerson,
  openPersonRenameModal,
  submitPersonRename,
};`).runInContext(sandbox);

const {
  state,
  revealPeopleSuggestionRename,
  previewPeopleSuggestion,
  confirmPeopleSuggestionMerge,
  openSelectedPeopleMergeModal,
  submitMergePerson,
  openPersonRenameModal,
  submitPersonRename,
} = sandbox.__mergeExports;
let checks = 0;

state.peopleSuggestions = [{ names: ["Mike", "Michael"], target: "Michael" }];
elements.get("people-suggestions").dataset.target = "Michael";
revealPeopleSuggestionRename();
assert.equal(elements.get("people-suggestion-rename-panel").hidden, false);
assert.equal(elements.get("people-suggestion-target").value, "Michael");
assert.equal(elements.get("people-suggestion-target").focused, true);
checks += 1;

const calls = [];
sandbox.fetch = async (url, options = {}) => {
  calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
  if (url === "/api/people/merge-preview") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        digest: "preview-digest",
        requested_target: "Mikael",
        actual_target: "Mikael",
        affected_meetings: 3,
        files_changed: 2,
        literal_matches: 4,
        missing_files: [],
        conflicts: [],
      }),
    };
  }
  if (url === "/api/people/merge-many") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        target: "Mikael",
        minutes_rewritten: 2,
        minutes_unchanged: 1,
        minutes_missing: 0,
        rewrite_conflicts: 0,
        pending_rewrites: 0,
      }),
    };
  }
  return { ok: false, status: 503, json: async () => ({}) };
};

elements.get("people-suggestions").dataset.names = JSON.stringify(["Mike", "Michael"]);
elements.get("people-suggestion-target").value = "Mikael";
await previewPeopleSuggestion();
assert.deepEqual(calls[0], {
  url: "/api/people/merge-preview",
  body: { names: ["Mike", "Michael"], into: "Mikael" },
});
assert.equal(elements.get("people-suggestion-confirm").disabled, false);

await confirmPeopleSuggestionMerge();
const mutation = calls.find((call) => call.url === "/api/people/merge-many");
assert.deepEqual(mutation.body, {
  names: ["Mike", "Michael"],
  into: "Mikael",
  expected_digest: "preview-digest",
});
assert.equal(state.peopleSuggestions.length, 0);
checks += 1;

let releasePreview;
let previewCalls = 0;
sandbox.fetch = (url) => {
  assert.equal(url, "/api/people/merge-preview");
  previewCalls += 1;
  return new Promise((resolve) => {
    releasePreview = () => resolve({
      ok: true,
      status: 200,
      json: async () => ({
        digest: "busy-preview",
        requested_target: "Michael",
        actual_target: "Michael",
        affected_meetings: 1,
        files_changed: 0,
        literal_matches: 0,
        missing_files: [],
        conflicts: [],
      }),
    });
  });
};
state.peopleSuggestions = [{ names: ["Mike", "Michael"], target: "Michael" }];
state.peopleSuggestionMergePreview = null;
elements.get("people-suggestions").dataset.names = JSON.stringify(["Mike", "Michael"]);
elements.get("people-suggestions").dataset.target = "Michael";
elements.get("people-suggestion-target").value = "Michael";
const firstPreview = previewPeopleSuggestion();
const duplicatePreview = await previewPeopleSuggestion();
assert.equal(duplicatePreview, false);
assert.equal(previewCalls, 1);
assert.equal(elements.get("people-suggestion-target").disabled, true);
releasePreview();
await firstPreview;
assert.equal(elements.get("people-suggestion-target").disabled, false);
checks += 1;

state.peopleSuggestions = [{ names: ["Mike", "Michael"], target: "Michael" }];
state.peopleSuggestionMergePreview = {
  digest: "stale-digest",
  requested_target: "Michael",
};
sandbox.fetch = async () => ({
  ok: false,
  status: 409,
  json: async () => ({ error: "preview changed" }),
});
const staleApplied = await confirmPeopleSuggestionMerge();
assert.equal(staleApplied, false);
assert.equal(state.peopleSuggestions.length, 1);
assert.equal(state.peopleSuggestionMergePreview, null);
assert.equal(elements.get("people-suggestion-confirm").disabled, true);
assert.match(elements.get("people-suggestion-preview").textContent, /stale/i);
checks += 1;

state.peopleSuggestionMergePreview = {
  digest: "valid-digest",
  requested_target: "Michael",
};
sandbox.fetch = async () => ({
  ok: false,
  status: 500,
  json: async () => ({ error: "disk refused" }),
});
const failedApplied = await confirmPeopleSuggestionMerge();
assert.equal(failedApplied, false);
assert.equal(state.peopleSuggestions.length, 1);
assert.equal(elements.get("people-suggestion-confirm").disabled, false);
assert.equal(elements.get("people-suggestion-target").disabled, false);
checks += 1;

const modalCalls = [];
sandbox.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  modalCalls.push({ url, body });
  if (url === "/api/people/merge-preview") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        digest: "modal-digest",
        requested_target: "Aimee",
        actual_target: "Aimee",
        affected_meetings: 2,
        files_changed: 1,
        literal_matches: 3,
        missing_files: [],
        conflicts: [],
      }),
    };
  }
  if (url === "/api/people/merge-many") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        target: "Aimee",
        minutes_rewritten: 1,
        minutes_unchanged: 0,
        minutes_missing: 0,
        rewrite_conflicts: 0,
        pending_rewrites: 0,
      }),
    };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
state.selectedPeople = new Set(["Zoe", "Amy"]);
openSelectedPeopleMergeModal();
assert.deepEqual(
  JSON.parse(elements.get("person-merge-sources").value),
  ["Amy", "Zoe"],
);
elements.get("person-merge-target").value = "Amy";
elements.get("person-merge-target-custom").value = "Aimee";
await submitMergePerson();
assert.deepEqual(modalCalls[0], {
  url: "/api/people/merge-preview",
  body: { names: ["Amy", "Zoe"], into: "Aimee" },
});
assert.match(elements.get("person-merge-save").textContent, /confirm/i);
await submitMergePerson();
assert.deepEqual(modalCalls[1], {
  url: "/api/people/merge-many",
  body: {
    names: ["Amy", "Zoe"],
    into: "Aimee",
    expected_digest: "modal-digest",
  },
});
checks += 1;

const renameCalls = [];
sandbox.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  renameCalls.push({ url, body });
  if (url === "/api/people/merge-preview") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        digest: "rename-digest",
        requested_target: "John",
        actual_target: "John",
        affected_meetings: 1,
        files_changed: 1,
        literal_matches: 2,
        missing_files: [],
        conflicts: [],
      }),
    };
  }
  if (url === "/api/people/rename") {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        target: "John",
        minutes_rewritten: 1,
        minutes_unchanged: 0,
        minutes_missing: 0,
        rewrite_conflicts: 0,
        pending_rewrites: 0,
      }),
    };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
openPersonRenameModal("Jon");
elements.get("person-rename-name").value = "John";
await submitPersonRename();
assert.deepEqual(renameCalls[0], {
  url: "/api/people/merge-preview",
  body: { names: ["Jon"], into: "John" },
});
await submitPersonRename();
assert.deepEqual(renameCalls[1], {
  url: "/api/people/rename",
  body: {
    from_name: "Jon",
    new_name: "John",
    expected_digest: "rename-digest",
  },
});
checks += 1;

console.log(`${checks} checks passed`);
