/**
 * Meeting Memory — Executive Archive & Interactive Assistant
 */

let state = {
  activeMeetingId: null,
  meetings: [],
  people: [],
  peopleSuggestions: [],
  selectedPeople: new Set(),
  peopleSuggestionMergePreview: null,
  peopleSuggestionMergeBusy: false,
  personMergePreview: null,
  personMergeBusy: false,
  personRenamePreview: null,
  personRenameBusy: false,
  pipelineRunning: false,
  // Set on click and cleared when /api/pipeline/run answers. Without it a
  // second click in that window slipped past the pipelineRunning guard.
  pipelineStarting: false,
  // Last `running` value the server actually reported. pipelineRunning is
  // also set optimistically, so the finished-run announcement below needs a
  // flag that only ever moves on a real status response.
  lastServerRunning: null,
  pollTimer: null,
};

// ── Category vocabulary ───────────────────────────────────────────────
// One source of truth for how a domain is spelled on screen. The same stored
// value used to render three different ways in one view - "Work" on the card,
// "Professional" in the filter, "Professional / Work" in the detail select -
// which read as three taxonomies instead of one. The backend stores exactly
// two domains; the sub-type is a separate, secondary fact and is shown as its
// own chip rather than being folded into the domain label.
const CATEGORY_DOMAINS = {
  Professional: { label: "Professional", icon: "💼", fg: "#166534", bg: "#f0fdf4", border: "#bbf7d0" },
  Personal: { label: "Personal", icon: "🏠", fg: "#86198f", bg: "#fdf4ff", border: "#f5d0fe" },
};
const categoryDomain = (value) => CATEGORY_DOMAINS[value] || CATEGORY_DOMAINS.Professional;
// Sub-types that restate the domain carry no information, so they stay hidden.
const GENERIC_SUBTYPES = new Set(["", "general", "work", "professional", "personal", "personal & household"]);

const CHAT_SESSION_STORAGE_KEY = "meeting-memory-chat-session";

function getChatSessionId() {
  let sessionId = localStorage.getItem(CHAT_SESSION_STORAGE_KEY);
  if (!sessionId) {
    sessionId = crypto.randomUUID().replaceAll("-", "");
    localStorage.setItem(CHAT_SESSION_STORAGE_KEY, sessionId);
  }
  return sessionId;
}

function setChatSessionId(sessionId) {
  localStorage.setItem(CHAT_SESSION_STORAGE_KEY, sessionId);
}

function setupAskPanel() {
  const actions = document.querySelector("#query-form .query-actions");
  if (!actions || $("btn-new-conversation")) return;
  const button = document.createElement("button");
  button.type = "button";
  button.id = "btn-new-conversation";
  button.className = "btn-outline";
  button.textContent = "New conversation";
  button.addEventListener("click", startNewChatSession);
  actions.prepend(button);
}

async function startNewChatSession() {
  try {
    const res = await fetch("/api/query/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: getChatSessionId() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not start a new conversation");
    setChatSessionId(data.session_id);
    $("question").value = "";
    $("answer").className = "answer empty";
    $("answer").innerHTML = "<p>Type a question above to start a new conversation.</p>";
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ── DOM Helpers ───────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Pending-action feedback ──────────────────────────────────────────
// Every action here is a round trip to a local server that may be calling an
// LLM, transcribing audio, or rewriting minutes on disk. A click used to
// produce nothing visible until the request finished, so a slow success and a
// dead button looked identical: the only honest signal was the toast, which
// arrives seconds or minutes later. These helpers give the clicked control a
// disabled, aria-busy pending state for the whole round trip, and double as
// the double-submit guard.
//
// The inline `onclick` handlers in index.html cannot hand their own button to
// the function they call without editing markup the UI contract tests pin, so
// a capture-phase listener records it instead. Capture on document runs before
// the target's own inline handler, so `triggerButton()` is already accurate by
// the time that handler asks for it.
let lastClickedButton = null;
document.addEventListener(
  "click",
  (event) => {
    lastClickedButton = event.target instanceof Element ? event.target.closest("button") : null;
  },
  true,
);

const triggerButton = () => lastClickedButton;

function setButtonBusy(button, label = null) {
  if (!button || button.dataset.busy === "1") return false;
  button.dataset.busy = "1";
  // Some controls are disabled for their own reasons - "Merge selected" needs
  // two selections - so the prior state is restored, not assumed to be false.
  button.dataset.idleDisabled = button.disabled ? "1" : "0";
  button.setAttribute("aria-busy", "true");
  // Stage buttons wrap a <strong> and a <span>; swapping textContent would
  // flatten them, so a label is only applied when the caller asks for one.
  if (label !== null) {
    button.dataset.idleLabel = button.textContent;
    button.textContent = label;
  }
  button.disabled = true;
  return true;
}

function clearButtonBusy(button) {
  if (!button || button.dataset.busy !== "1") return;
  if (button.dataset.idleLabel !== undefined) {
    button.textContent = button.dataset.idleLabel;
    delete button.dataset.idleLabel;
  }
  button.disabled = button.dataset.idleDisabled === "1";
  delete button.dataset.idleDisabled;
  delete button.dataset.busy;
  button.removeAttribute("aria-busy");
}

// Returns undefined without running `task` when the control is already busy.
// That early return is what stops a second click firing a duplicate request.
async function withBusy(button, label, task) {
  if (button && button.dataset.busy === "1") return undefined;
  setButtonBusy(button, label);
  try {
    return await task();
  } finally {
    clearButtonBusy(button);
  }
}

// (Initialization is handled by init() at the bottom of this file)

// ── Tabs Navigation ──────────────────────────────────────────────────
function setupTabs() {
  const tabs = [...$$(".tab-btn")];
  tabs.forEach((btn, index) => {
    btn.addEventListener("click", () => activateTab(btn));
    btn.addEventListener("keydown", (event) => {
      const destinations = {
        ArrowLeft: (index - 1 + tabs.length) % tabs.length,
        ArrowRight: (index + 1) % tabs.length,
        Home: 0,
        End: tabs.length - 1,
      };
      if (!(event.key in destinations)) return;
      event.preventDefault();
      activateTab(tabs[destinations[event.key]], true);
    });
  });
}

function activateTab(tab, moveFocus = false) {
  $$(".tab-btn").forEach((button) => {
    const selected = button === tab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("tabindex", selected ? "0" : "-1");
  });
  $$(".tab-pane").forEach((pane) => pane.classList.toggle("active", pane.id === tab.dataset.tab));
  if (moveFocus) tab.focus();
}

function setupAskModeTabs() {
  const tabs = [$("mode-btn-qa"), $("mode-btn-timeline")].filter(Boolean);
  tabs.forEach((tab, index) => {
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const destination = event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : event.key === "ArrowLeft"
            ? (index - 1 + tabs.length) % tabs.length
            : (index + 1) % tabs.length;
      const next = tabs[destination];
      switchAskMode(next === $("mode-btn-qa") ? "qa" : "timeline");
      next.focus();
    });
  });
}

function setupEventListeners() {
  // Search & Filter
  $("meeting-search").addEventListener("input", () => filterAndRenderMeetings());
  // The archive controls re-render the meeting list; the speaker controls
  // re-render the review queue. Both save first, so the choice survives the
  // reload - this queue gets worked over days, not in one sitting.
  ["meeting-sort", "meeting-filter-range", "meeting-filter-audio", "meeting-filter-status"].forEach(
    (id) => {
      const control = $(id);
      if (control) {
        control.addEventListener("change", () => {
          saveControlState();
          filterAndRenderMeetings();
        });
      }
    },
  );
  ["speaker-sort", "speaker-filter-band", "speaker-filter-suggestion", "speaker-filter-clip"].forEach(
    (id) => {
      const control = $(id);
      if (control) {
        control.addEventListener("change", () => {
          saveControlState();
          applySpeakerControls();
        });
      }
    },
  );
  if ($("meeting-filter-category")) {
    $("meeting-filter-category").addEventListener("change", () => {
      saveControlState();
      filterAndRenderMeetings();
    });
  }

  // Pipeline, export & refresh
  $("btn-quick-run").addEventListener("click", (event) => runStage("all", event.currentTarget));
  $("btn-export-pm").addEventListener("click", exportToProductManager);
  $("btn-refresh").addEventListener("click", (event) =>
    // The toast used to fire while all of these requests were still in flight,
    // so it reported a refresh that had not happened yet.
    withBusy(event.currentTarget, "↻ Refreshing…", async () => {
      await Promise.all([
        loadOverview(),
        loadMeetings(),
        loadPeople(),
        loadCommitments(),
        loadVoiceClusters(),
        checkPipelineStatus(),
      ]);
      showToast("Refreshed archive data", "success");
    }),
  );

  // Query / RAG Form
  $("query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    askArchive();
  });
  $("timeline-form").addEventListener("submit", (e) => {
    e.preventDefault();
    loadTimelineForTopic($("timeline-topic-input").value, e.submitter);
  });

  $("form-add-person").addEventListener("submit", (e) => {
    e.preventDefault();
    submitAddPerson(e.submitter);
  });
  $("person-merge-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitMergePerson();
  });
  $("person-merge-target").addEventListener("change", updatePersonMergePreview);
  $("person-merge-target-custom").addEventListener("input", updatePersonMergePreview);
  $("people-search").addEventListener("input", () => renderPeopleList(state.people));
  $("people-merge-selected").addEventListener("click", openSelectedPeopleMergeModal);
  $("people-clear-selection").addEventListener("click", clearPeopleSelection);
  $("person-rename-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitPersonRename();
  });
  $("person-rename-name").addEventListener("input", invalidatePersonRenamePreview);
  $("people-suggestion-yes").addEventListener("click", acceptPeopleSuggestion);
  $("people-suggestion-rename").addEventListener("click", revealPeopleSuggestionRename);
  $("people-suggestion-review").addEventListener("click", previewPeopleSuggestion);
  $("people-suggestion-confirm").addEventListener("click", confirmPeopleSuggestionMerge);
  $("people-suggestion-target").addEventListener("input", invalidatePeopleSuggestionPreview);
  $("people-suggestion-no").addEventListener("click", dismissPeopleSuggestion);
  $("speaker-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitSpeakerModal(e.submitter);
  });
  $("voice-name-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitVoiceNameModal();
  });
  $$('input[name="voice-name-mode"]').forEach((radio) => {
    radio.addEventListener("change", updateVoiceNameMode);
  });

  // Diagnostics Drawer: stage failure history is queried on open, not on the
  // 8s overview poll - nobody is looking at it most of the time.
  const diagnosticsDrawer = document.querySelector(".diagnostics-drawer");
  if (diagnosticsDrawer) {
    diagnosticsDrawer.addEventListener("toggle", () => {
      if (diagnosticsDrawer.open) loadStageFailures();
    });
  }
}

// ── Overview & Metrics ───────────────────────────────────────────────
async function loadOverview() {
  try {
    const res = await fetch("/api/overview");
    if (!res.ok) throw new Error("Overview unavailable");
    const data = await res.json();
    renderOverview(data);
  } catch (err) {
    $("rag-health").textContent = "AI Search: Offline";
    console.error("Failed to load overview:", err);
  }
}

function renderOverview(data) {
  // Masthead & Signal Strip
  const isHealthy = data.lightrag === "ok" || data.lightrag === "healthy";
  $("rag-health").textContent = isHealthy ? "AI Search: Ready" : "AI Search: Offline (Docker)";
  
  $("metric-meetings").textContent = data.meetings ?? "0";
  $("metric-indexed").textContent = data.indexed ?? "0";

  const yest = data.activity?.yesterday || {};
  $("metric-yesterday").textContent = `${yest.count || 0} (${yest.duration_min || 0}m)`;

  const today = data.activity?.today || {};
  $("metric-today").textContent = `${today.count || 0} (${today.duration_min || 0}m)`;

  $("metric-hours").textContent = `${data.durations?.total_hours || 0} hrs`;
  const failedCount = data.failed || 0;
  const speakerReviewCount = data.speaker_review || 0;
  const attentionCount = failedCount + speakerReviewCount;
  const attentionParts = [];
  if (failedCount) attentionParts.push(`${failedCount} failed`);
  if (speakerReviewCount) attentionParts.push(`${speakerReviewCount} speaker labels`);
  $("metric-attention").textContent = attentionCount ? `${attentionCount} items` : "None";
  const archiveAttention = $("archive-attention");
  archiveAttention.classList.toggle("needs-attention", attentionCount > 0);
  archiveAttention.textContent = attentionCount
    ? `Archive Review: ${attentionParts.join(" · ")}`
    : "Archive Ready";

  // Queue Funnel
  const queue = data.queue || {};
  $("funnel-drive").textContent = data.drive?.total_files || 0;
  $("funnel-discovered").textContent = queue.discovered || 0;
  $("funnel-transcribed").textContent = queue.transcribed || 0;
  $("funnel-resolved").textContent = queue.speakers_resolved || 0;
  $("funnel-compiled").textContent = queue.minutes_compiled || 0;
  $("funnel-indexed").textContent = queue.indexed || 0;

  // Throughput Velocity Table
  const last7 = data.activity?.last_7_days || {};
  const dur = data.durations || {};

  $("vel-today-count").textContent = today.count || 0;
  $("vel-today-min").textContent = `${today.duration_min || 0} min`;
  $("vel-today-hrs").textContent = `${today.duration_hours || 0} hrs`;

  $("vel-yesterday-count").textContent = yest.count || 0;
  $("vel-yesterday-min").textContent = `${yest.duration_min || 0} min`;
  $("vel-yesterday-hrs").textContent = `${yest.duration_hours || 0} hrs`;

  $("vel-week-count").textContent = last7.count || 0;
  $("vel-week-min").textContent = `${last7.duration_min || 0} min`;
  $("vel-week-hrs").textContent = `${last7.duration_hours || 0} hrs`;

  $("vel-total-count").textContent = data.meetings || 0;
  $("vel-total-min").textContent = `${dur.total_min || 0} min`;
  $("vel-total-hrs").textContent = `${dur.total_hours || 0} hrs`;

  // Stage Timings Table (Diagnostics)
  renderTimingsTable(data.timings || []);

  // Top Discussion Topics
  renderEntitiesTable(data.knowledge?.top_entities || []);

  // Drive Stats
  $("drive-stat-files").textContent = data.drive?.total_files || 0;
  $("drive-stat-bytes").textContent = `${data.drive?.total_mb || 0} MB`;
  $("drive-stat-ingested").textContent = data.drive?.by_state?.ingested?.count || 0;
  const staged = (data.drive?.by_state?.staged?.count || 0) + (data.drive?.by_state?.excluded?.count || 0);
  $("drive-stat-other").textContent = staged;
}

function renderTimingsTable(timings) {
  const tbody = $("timings-tbody");
  if (!tbody) return;
  if (!timings.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-cell">No stage runs recorded yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = timings
    .map(
      (t) => `
    <tr>
      <td><strong>${escapeHtml(t.stage)}</strong></td>
      <td>${t.runs}</td>
      <td>${t.ok_runs}</td>
      <td>${formatSeconds(t.avg_sec)}</td>
      <td>${formatSeconds(t.max_sec)}</td>
    </tr>
  `
    )
    .join("");
}

function renderEntitiesTable(entities) {
  const tbody = $("entities-tbody");
  if (!tbody) return;
  if (!entities.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-cell">No discussion topics recorded yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = entities
    .map(
      (e) => `
    <tr>
      <td><strong>${escapeHtml(e.name)}</strong></td>
      <td><span class="chip">${escapeHtml(e.kind || "topic")}</span></td>
      <td>${e.meetings || e.mention_count || 1} meetings</td>
    </tr>
  `
    )
    .join("");
}

// ── Stage Failure History (Diagnostics) ──────────────────────────────
// A stage that fails and is later retried successfully leaves the meeting at
// a healthy status - overview()'s `failed` count only ever reflects meetings
// failing RIGHT NOW. This is fetched lazily, only when the diagnostics drawer
// is actually opened, so it never joins the 8s overview poll.
let stageFailuresSectionBuilt = false;

function ensureStageFailuresSection() {
  if (stageFailuresSectionBuilt) return;
  const drawer = document.querySelector(".diagnostics-drawer .drawer-content");
  if (!drawer) return;
  const apiCard = drawer.querySelector(".section-card:last-child");
  const card = document.createElement("div");
  card.className = "section-card";
  card.style.marginTop = "20px";
  card.innerHTML = `
    <h3>Recent Stage Failures</h3>
    <table class="data-table" id="stage-failures-table">
      <thead>
        <tr>
          <th>Meeting</th>
          <th>Stage</th>
          <th>When</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody id="stage-failures-tbody">
        <tr><td colspan="4" class="empty-cell">Loading failure history...</td></tr>
      </tbody>
    </table>
  `;
  if (apiCard) {
    drawer.insertBefore(card, apiCard);
  } else {
    drawer.appendChild(card);
  }
  stageFailuresSectionBuilt = true;
}

async function loadStageFailures() {
  ensureStageFailuresSection();
  try {
    const res = await fetch("/api/diagnostics/stage-failures");
    if (!res.ok) throw new Error("Stage failure history unavailable");
    const data = await res.json();
    renderStageFailures(data.failures || []);
  } catch (err) {
    console.error("Failed to load stage failure history:", err);
  }
}

function renderStageFailures(failures) {
  const tbody = $("stage-failures-tbody");
  if (!tbody) return;
  if (!failures.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No failed stage runs recorded.</td></tr>`;
    return;
  }
  tbody.innerHTML = failures
    .map(
      (f) => `
    <tr>
      <td>${escapeHtml(f.label)}</td>
      <td><strong>${escapeHtml(f.stage)}</strong></td>
      <td>${escapeHtml(f.finished_at || f.started_at || "")}</td>
      <td>${escapeHtml((f.detail || "").slice(0, 160))}</td>
    </tr>
  `
    )
    .join("");
}

// ── Pipeline Orchestrator & Live Status ──────────────────────────────
async function checkPipelineStatus() {
  try {
    const res = await fetch("/api/pipeline/status");
    if (!res.ok) return;
    handlePipelineStatus(await res.json());
  } catch (err) {
    console.error("Status check failed:", err);
  }
}

function handlePipelineStatus(status) {
  const indicator = $("pipeline-indicator");
  const statusText = $("pipeline-status-text");
  const terminalBody = $("terminal-body");
  const wasRunning = state.lastServerRunning === true;
  state.lastServerRunning = status.running;
  state.pipelineRunning = status.running;
  setStageControlsRunning(status.running);

  const stageFriendly = {
    all: "Processing All Stages",
    capture: "Checking Google Drive",
    ingest: "Discovering Audio",
    transcribe: "Transcribing Speech",
    speakers: "Matching Speakers",
    minutes: "Synthesizing Minutes",
    index: "Updating AI Search Index",
    recompile: "Refreshing Formats",
  };

  if (status.running) {
    const friendlyName = stageFriendly[status.stage] || status.stage;
    indicator.classList.add("running");
    statusText.textContent = `● ${friendlyName}...`;
    $("btn-quick-run").disabled = true;
    $("btn-quick-run").textContent = `⏳ ${friendlyName}...`;
  } else {
    indicator.classList.remove("running");
    statusText.textContent = "Processing Idle";
    // Same window as setStageControlsRunning: a poll that lands before the
    // start request answers must not offer the button back.
    $("btn-quick-run").disabled = state.pipelineStarting;
    $("btn-quick-run").textContent = "▶ Sync & Process Recordings";
  }

  if (terminalBody && status.logs && status.logs.length) {
    terminalBody.textContent = status.logs.join("\n");
    terminalBody.scrollTop = terminalBody.scrollHeight;
  }

  if (status.running && !state.pollTimer) {
    state.pollTimer = setTimeout(async () => {
      state.pollTimer = null;
      await checkPipelineStatus();
      loadOverview();
      loadMeetings();
    }, 1500);
  } else if (!status.running && state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  // A finished run used to say nothing at all: the badge quietly returned to
  // idle and a crash was visible only in the log drawer, which is closed by
  // default. That silence is where "did my click do anything?" came from.
  // Announce the outcome once, on the transition, and reload what the run
  // could have changed.
  if (wasRunning && !status.running) {
    if (status.error) {
      showToast(`Processing stopped: ${status.error}`, "error");
    } else if (status.success === false) {
      showToast("Processing finished with errors - open Diagnostics for the log.", "error");
    } else {
      showToast("Processing finished.", "success");
    }
    loadOverview();
    loadMeetings();
    loadPeople();
    loadVoiceClusters();
    loadCommitments();
  }
}

// Lock every control that starts pipeline work while the single background
// worker is busy. The backend rejects a concurrent run with a 400, so leaving
// these live only bought an error toast; disabling them says why up front.
function setStageControlsRunning(running) {
  // The 8s overview poll can land inside the window between the click and the
  // server reporting `running`. Without pipelineStarting in the test, that
  // poll would briefly re-enable the whole grid mid-start.
  const locked = running || state.pipelineStarting;
  const controls = [
    ...$$(".btn-stage"),
    document.querySelector(".speaker-refresh-box .btn-action"),
  ];
  controls.forEach((button) => {
    // A button mid-request owns its own disabled state; clearButtonBusy hands
    // it back to whatever this function last decided.
    if (button && button.dataset.busy !== "1") button.disabled = locked;
  });
}

async function runStage(stage, trigger = triggerButton()) {
  if (state.pipelineRunning || state.pipelineStarting) {
    showToast("Processing is already in progress.", "info");
    return;
  }
  state.pipelineStarting = true;
  // Lock the action grid on the click rather than on the next status poll up
  // to 1.5s later. That gap was long enough to read as a dead button.
  setStageControlsRunning(true);
  const started = await withBusy(trigger, null, async () => {
    try {
      const res = await fetch("/api/pipeline/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to start operation");
      const stageMessage = stage === "speaker-refresh"
        ? "Updating corrected minutes and AI search"
        : `Started audio processing (${stage})`;
      showToast(stageMessage, "success");
      return true;
    } catch (err) {
      showToast(err.message, "error");
      return false;
    }
  });
  state.pipelineStarting = false;
  // A run that finishes between here and the first poll would otherwise never
  // announce itself, because the transition to idle was never observed.
  if (started) state.lastServerRunning = true;
  else setStageControlsRunning(false);
  checkPipelineStatus();
}

async function retryAllFailed(trigger = triggerButton()) {
  await withBusy(trigger, null, async () => {
    try {
      const res = await fetch("/api/pipeline/retry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "discovered" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Retry failed");
      showToast(`Requeued ${data.requeued} recordings for processing`, "success");
      await Promise.all([loadOverview(), loadMeetings()]);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

async function recompileStale(trigger = triggerButton()) {
  if (state.pipelineRunning || state.pipelineStarting) {
    showToast("Processing is already in progress.", "info");
    return;
  }
  state.pipelineStarting = true;
  setStageControlsRunning(true);
  const started = await withBusy(trigger, null, async () => {
    try {
      const res = await fetch("/api/pipeline/recompile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Format refresh failed");
      showToast("Refreshing minutes layout", "success");
      return true;
    } catch (err) {
      showToast(err.message, "error");
      return false;
    }
  });
  state.pipelineStarting = false;
  if (started) state.lastServerRunning = true;
  else setStageControlsRunning(false);
  checkPipelineStatus();
}

async function retrySingleMeeting(meetingId, trigger = triggerButton()) {
  await withBusy(trigger, "\u21bb Requeueing\u2026", async () => {
    try {
      const res = await fetch(`/api/meetings/${meetingId}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "discovered" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Retry failed");
      showToast("Meeting requeued for transcription", "success");
      await loadMeetings();
      selectMeeting(meetingId);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

function clearTerminalLogs() {
  const terminalBody = $("terminal-body");
  if (terminalBody) terminalBody.textContent = "Log view cleared.";
}

// ── Meetings Archive & Executive Reader ──────────────────────────────
async function loadMeetings() {
  try {
    const res = await fetch("/api/meetings");
    if (!res.ok) throw new Error("Meetings unavailable");
    const data = await res.json();
    state.meetings = data.meetings || [];
    filterAndRenderMeetings();
  } catch (err) {
    $("meeting-list").innerHTML = `<div class="empty-list">Could not load meetings.</div>`;
  }
}

// ── Sort & filter controls ────────────────────────────────────────────
// 104 meetings and 166 waiting speaker decisions are past what one fixed order
// can serve: the row worth opening next is "thinnest score margin" one minute
// and "longest speech I still have a clip for" the next. All of it runs on the
// client - the whole archive and the whole queue already arrive in one payload
// each, so re-sorting them costs no round trip.

const CONTROL_STORAGE_KEY = "mmc.controls";

const CONTROL_IDS = [
  "meeting-sort",
  "meeting-filter-range",
  "meeting-filter-audio",
  "meeting-filter-category",
  "meeting-filter-status",
  "speaker-sort",
  "speaker-filter-band",
  "speaker-filter-suggestion",
  "speaker-filter-clip",
];

// localStorage throws outright in a browser set to block site data, so every
// access is guarded: refusing to remember a choice must not stop a list from
// rendering.
function saveControlState() {
  try {
    const chosen = {};
    CONTROL_IDS.forEach((id) => {
      const control = $(id);
      if (control) chosen[id] = control.value;
    });
    localStorage.setItem(CONTROL_STORAGE_KEY, JSON.stringify(chosen));
  } catch (err) {
    // Not remembering is a smaller problem than a console full of noise.
  }
}

function loadControlState() {
  let stored = {};
  try {
    stored = JSON.parse(localStorage.getItem(CONTROL_STORAGE_KEY) || "{}");
  } catch (err) {
    return;
  }
  CONTROL_IDS.forEach((id) => {
    const control = $(id);
    const value = stored[id];
    // A stored value whose <option> has since been renamed would blank the
    // select entirely; falling back to the markup default keeps it populated.
    if (control && value != null && [...control.options].some((o) => o.value === value)) {
      control.value = value;
    }
  });
}

// Rows with nothing to compare sort last in BOTH directions. Treating a missing
// score as 0 would hand the top of every ascending list to the rows that were
// never scored, which is the opposite of triage order.
function compareBy(read, direction) {
  const sign = direction === "asc" ? 1 : -1;
  return (a, b) => {
    const left = read(a);
    const right = read(b);
    if (left == null && right == null) return 0;
    if (left == null) return 1;
    if (right == null) return -1;
    if (left === right) return 0;
    return left < right ? -sign : sign;
  };
}

const MEETING_SORTS = {
  "date-desc": compareBy((m) => (m.date ? `${m.date} ${m.time || ""}` : null), "desc"),
  "date-asc": compareBy((m) => (m.date ? `${m.date} ${m.time || ""}` : null), "asc"),
  "duration-desc": compareBy((m) => m.duration_sec, "desc"),
  "speakers-desc": compareBy((m) => m.speaker_count, "desc"),
  "unresolved-desc": compareBy((m) => m.unresolved_count, "desc"),
  "title-asc": (a, b) => (a.title || "").localeCompare(b.title || ""),
};

// A recurring cluster and a lone label are the same decision wearing two
// shapes: clusters count `size`/`total_speech` and hold their meetings in
// `members`, one-offs carry `speech_sec` and a single meeting inline. Reading
// both here means every comparator and predicate below speaks one vocabulary -
// otherwise "sort by speech time" silently sorts one of the two lists by
// undefined.
function normalizeSpeakerRow(row) {
  const members = row.members || [];
  const dates = members.map((m) => m.meeting_date).filter(Boolean);
  const best = row.best_score == null ? null : Number(row.best_score);
  const next = row.next_score == null ? null : Number(row.next_score);
  return {
    score: best,
    // Distance to the runner-up. Null when there is no second guess to be
    // close to: an unmatched voice is not a close call, it is no call, and it
    // does not belong at the top of "closest call first".
    margin: best == null || next == null ? null : Number((best - next).toFixed(4)),
    speech: row.total_speech != null ? row.total_speech : row.speech_sec || 0,
    occurrences: row.size != null ? row.size : 1,
    latestDate: dates.length ? dates.slice().sort().pop() : row.meeting_date || null,
    // Clips are cut once at enrollment and the source audio is deleted right
    // after transcription, so "has audio" here is permanent, not pending.
    hasClip: row.clip_seconds != null ? row.clip_seconds > 0 : (row.snippet_count || 0) > 0,
    band: row.band || null,
    hasVoiceprint: Boolean(row.best_canonical),
    hasTranscriptName: Boolean(row.llm_suggestion),
  };
}

const SPEAKER_SORTS = {
  "margin-asc": compareBy((r) => normalizeSpeakerRow(r).margin, "asc"),
  "confidence-desc": compareBy((r) => normalizeSpeakerRow(r).score, "desc"),
  "confidence-asc": compareBy((r) => normalizeSpeakerRow(r).score, "asc"),
  "speech-desc": compareBy((r) => normalizeSpeakerRow(r).speech, "desc"),
  "occurrences-desc": compareBy((r) => normalizeSpeakerRow(r).occurrences, "desc"),
  "date-desc": compareBy((r) => normalizeSpeakerRow(r).latestDate, "desc"),
};

function speakerMatchesFilters(row, controls) {
  const facts = normalizeSpeakerRow(row);
  if (controls.band !== "all" && facts.band !== controls.band) return false;
  if (controls.clip === "yes" && !facts.hasClip) return false;
  if (controls.clip === "no" && facts.hasClip) return false;
  if (controls.suggestion === "voiceprint" && !facts.hasVoiceprint) return false;
  if (controls.suggestion === "transcript" && !facts.hasTranscriptName) return false;
  if (controls.suggestion === "none" && (facts.hasVoiceprint || facts.hasTranscriptName)) {
    return false;
  }
  return true;
}

function readSpeakerControls() {
  const value = (id, fallback) => ($(id) ? $(id).value : fallback);
  return {
    sort: value("speaker-sort", "margin-asc"),
    band: value("speaker-filter-band", "all"),
    suggestion: value("speaker-filter-suggestion", "all"),
    clip: value("speaker-filter-clip", "all"),
  };
}

function applySpeakerControls() {
  const controls = readSpeakerControls();
  const comparator = SPEAKER_SORTS[controls.sort] || SPEAKER_SORTS["margin-asc"];
  const shape = (rows) =>
    (rows || []).filter((row) => speakerMatchesFilters(row, controls)).sort(comparator);
  const clusters = shape(state.voiceClusters);
  const oneOffs = shape(state.unresolvedSpeakers);

  // Say what the filters removed. A shrunken list with no count reads as
  // "there is nothing left to do", which is the one thing it must never mean.
  const total = (state.voiceClusters || []).length + (state.unresolvedSpeakers || []).length;
  const shown = clusters.length + oneOffs.length;
  const readout = $("speaker-visible-count");
  if (readout) {
    readout.textContent =
      shown === total
        ? `${total} decision${total === 1 ? "" : "s"} waiting`
        : `${shown} of ${total} shown \u00b7 ${total - shown} hidden by filters`;
  }

  renderVoiceCards(clusters, oneOffs);
}

// Cutoffs are resolved once per render, never once per row.
function isoDaysAgo(days) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toISOString().slice(0, 10);
}

function filterAndRenderMeetings() {
  const query = ($("meeting-search").value || "").toLowerCase().trim();
  const filter = $("meeting-filter-status").value;
  const categoryFilter = $("meeting-filter-category") ? $("meeting-filter-category").value : "all";
  const rangeFilter = $("meeting-filter-range") ? $("meeting-filter-range").value : "all";
  const audioFilter = $("meeting-filter-audio") ? $("meeting-filter-audio").value : "all";
  const sortKey = $("meeting-sort") ? $("meeting-sort").value : "date-desc";
  const cutoff = rangeFilter === "all" ? null : isoDaysAgo(Number(rangeFilter));

  const filtered = state.meetings.filter((m) => {
    if (categoryFilter !== "all" && m.category !== categoryFilter) return false;
    if (filter === "ready" && m.status !== "indexed") return false;
    if (filter === "review" && m.unresolved_count === 0) return false;
    if (filter === "failed" && m.status !== "failed") return false;
    // A meeting whose date never parsed sits on neither side of a date window,
    // so it stays visible rather than vanishing: hiding unparseable rows behind
    // a date filter is how a stuck meeting goes unnoticed for a month. The
    // status filter is the tool for putting those away.
    if (cutoff && m.date && m.date < cutoff) return false;
    if (audioFilter === "yes" && !m.has_audio) return false;
    if (audioFilter === "no" && m.has_audio) return false;

    if (!query) return true;
    return (
      (m.title || "").toLowerCase().includes(query) ||
      (m.date || "").toLowerCase().includes(query) ||
      (m.formatted_date || "").toLowerCase().includes(query) ||
      (m.formatted_time || "").toLowerCase().includes(query) ||
      (m.category || "").toLowerCase().includes(query) ||
      (m.category_type || "").toLowerCase().includes(query) ||
      (m.source_name || "").toLowerCase().includes(query) ||
      (m.excerpt || "").toLowerCase().includes(query)
    );
  });

  renderMeetingList(filtered.sort(MEETING_SORTS[sortKey] || MEETING_SORTS["date-desc"]));
}

function renderMeetingList(meetings) {
  const container = $("meeting-list");
  if (!meetings.length) {
    container.innerHTML = `<div class="empty-list">No meetings match your filters.</div>`;
    return;
  }

  container.innerHTML = meetings
    .map((m) => {
      const activeClass = m.id === state.activeMeetingId ? "active" : "";
      const isFailed = m.status === "failed";
      const isPersonal = m.category === "Personal";
      
      let statusBadge = "";
      if (isFailed) statusBadge = `<span class="badge warn">Needs Attention</span>`;
      else if (m.unresolved_count > 0) statusBadge = `<span class="badge" style="background:#fef3c7;color:#92400e;border:1px solid #fcd34d;">${m.unresolved_count} Unnamed Speaker${m.unresolved_count > 1 ? "s" : ""}</span>`;

      const domain = categoryDomain(m.category);
      const subType = (m.category_type || "").trim();
      const showSubType = subType && !GENERIC_SUBTYPES.has(subType.toLowerCase());
      const categoryBadge =
        `<span class="badge" style="background:${domain.bg};color:${domain.fg};border:1px solid ${domain.border};font-size:0.75rem;padding:2px 6px;">${domain.icon} ${domain.label}</span>` +
        (showSubType
          ? `<span class="badge" style="background:transparent;color:var(--muted);border:1px solid var(--rule);font-size:0.75rem;padding:2px 6px;">${escapeHtml(subType)}</span>`
          : "");

      const displayDate = m.formatted_date || formatShortDate(m.date);
      const displayTime = m.formatted_time || m.time || "";

      return `
      <button type="button" class="meeting-card ${activeClass}" onclick="selectMeeting('${m.id}')" style="display:block; text-align:left; padding:14px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
          <div style="display:flex; align-items:center; gap:6px;">
            <strong style="font-size:0.875rem; color:var(--ink); font-weight:700;">📅 ${escapeHtml(displayDate)}</strong>
            ${displayTime ? `<span style="font-size:0.8rem; color:var(--text-sub); font-weight:600;">🕒 ${escapeHtml(displayTime)}</span>` : ''}
          </div>
          ${categoryBadge}
        </div>
        <h3 style="margin:2px 0 6px 0; font-size:1.025rem; line-height:1.35; color:var(--ink);">${escapeHtml(m.title)}</h3>
        <p style="margin:0 0 8px 0; font-size:0.825rem; color:var(--text-sub); line-height:1.4;">${escapeHtml(m.excerpt || "No summary available.")}</p>
        <div style="display:flex; gap:6px; flex-wrap:wrap; align-items:center;">
          ${statusBadge}
          <span class="badge" style="background:var(--paper);border:1px solid var(--rule);">${formatDuration(m.duration_sec)}</span>
          ${!m.has_audio ? '<span class="badge" style="background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0;font-size:0.7rem;">Text Only</span>' : ''}
        </div>
      </button>
    `;
    })
    .join("");

  if (!state.activeMeetingId && meetings.length > 0) {
    selectMeeting(meetings[0].id);
  }
}

async function selectMeeting(id) {
  state.activeMeetingId = id;
  $$(".meeting-card").forEach((card) => card.classList.remove("active"));
  const foundCard = Array.from($$(".meeting-card")).find((c) =>
    c.getAttribute("onclick")?.includes(id)
  );
  if (foundCard) foundCard.classList.add("active");

  const reader = $("meeting-reader");
  reader.innerHTML = `<p class="reader-kicker">LOADING EXECUTIVE BRIEF...</p>`;

  try {
    const res = await fetch(`/api/meetings/${id}`);
    if (!res.ok) throw new Error("Meeting detail unavailable");
    const m = await res.json();
    renderMeetingDetail(m);
  } catch (err) {
    reader.innerHTML = `<div class="empty-list">Failed to load meeting brief.</div>`;
  }
}

function renderMeetingDetail(m) {
  const reader = $("meeting-reader");
  const isFailed = m.status === "failed";
  const displayDate = m.formatted_date || m.date || "Undated";
  const displayTime = m.formatted_time || m.time || "";
  const weekday = m.weekday ? `${m.weekday}, ` : "";
  const isPersonal = m.category === "Personal";

  let failedBanner = "";
  if (isFailed) {
    failedBanner = `
      <div class="failed-banner">
        <div>
          <strong>Processing Interrupted:</strong>
          <p>${escapeHtml(m.error || "An issue occurred during transcription.")}</p>
        </div>
        <button type="button" class="btn-action btn-warn" onclick="retrySingleMeeting('${m.id}')">↻ Retry Meeting</button>
      </div>
    `;
  }

  const categoryControl = `
    <div style="display:flex; align-items:center; gap:8px; background:var(--paper); border:1px solid var(--rule); padding:4px 10px; border-radius:6px;">
      <label for="meeting-category" style="font-size:12px; font-weight:600; color:var(--ink);">Category:</label>
      <select id="meeting-category" onchange="updateMeetingCategory('${m.id}', this.value, this)" style="background:transparent;border:0;color:var(--ink);font-weight:600;">
        <option value="Professional" ${isPersonal ? "" : "selected"}>💼 Professional / Work</option>
        <option value="Personal" ${isPersonal ? "selected" : ""}>🏠 Personal</option>
      </select>
    </div>
  `;

  const managementBar = `
    <div class="reader-mgmt-bar" style="display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:12px 0 16px 0; padding:8px 12px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px;">
      <span style="font-size:11.5px; color:#334155; margin-right:auto;">Meeting management</span>
      <button type="button" class="btn-outline btn-sm" onclick="promptDelete('${m.id}', 'audio', '${escapeHtml(m.title)}')">Delete Local Audio</button>
      <button type="button" class="btn-warn btn-sm" onclick="promptDelete('${m.id}', 'all', '${escapeHtml(m.title)}')">Delete Entire Meeting</button>
    </div>
  `;

  const driveLink = m.drive_url
    ? `<div class="reader-actions" style="margin-bottom:12px;"><a href="${escapeHtml(m.drive_url)}" target="_blank" rel="noopener">🎧 Listen to Original Audio in Google Drive ↗</a></div>`
    : "";

  const speakersHtml = (m.speakers || [])
    .map((s) => {
      const isUnresolved = !s.name;
      const chipClass = isUnresolved ? "chip chip-speaker unresolved" : "chip chip-speaker";
      const displayName = s.name ? `👤 ${s.name} (${s.label})` : `🎙️ ${s.label} · Click to name`;
      return `<button type="button" class="${chipClass} js-speaker-chip" data-meeting="${escapeHtml(m.id)}" data-label="${escapeHtml(s.label)}" data-name="${escapeHtml(s.name || "")}">${escapeHtml(displayName)}</button>`;
    })
    .join("");

  const entitiesHtml = (m.entities || [])
    .map((e) => `<span class="chip" title="${escapeHtml(e.description || "")}">${escapeHtml(e.name)}</span>`)
    .join("");

  const formattedMinutes = renderMarkdown(m.minutes || "No executive brief recorded for this meeting yet.");

  reader.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; gap:16px; flex-wrap:wrap;">
      <div>
        <div style="font-size:1.1rem; font-weight:700; color:var(--accent); letter-spacing:-0.01em; margin-bottom:2px;">
          📅 ${escapeHtml(weekday)}${escapeHtml(displayDate)} ${displayTime ? `· 🕒 ${escapeHtml(displayTime)}` : ""}
        </div>
        <span style="font-size:0.8rem; color:var(--text-sub); font-weight:500;">Recorded duration: ${formatDuration(m.duration_sec)}</span>
      </div>
      ${categoryControl}
    </div>

    <h2 style="font-size:1.6rem; line-height:1.25; margin:6px 0 10px 0;">${escapeHtml(m.title)}</h2>
    
    <div class="reader-meta" style="margin-bottom:10px;">
      <span>Meeting ID: <code>${m.short_id}</code></span>
      <span>Source: ${escapeHtml(m.source_name || "direct recording")}</span>
      <span>Status: <strong>${escapeHtml(m.status === "indexed" ? "Search Ready" : m.status)}</strong></span>
    </div>

    ${failedBanner}
    ${managementBar}
    ${driveLink}

    <div class="speaker-section">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px;">
        <p class="eyebrow" style="margin:0;">ATTENDEES &amp; SPEAKERS · CLICK TO NAME</p>
        <button type="button" class="btn-outline btn-sm" onclick="openSpeakerModal('${m.id}', '', '')">+ Add Attendee / Speaker</button>
      </div>
      <div class="speaker-list">
        ${speakersHtml || '<span class="chip" style="opacity:0.75;">No speakers diarized yet.</span>'}
      </div>
    </div>

    ${
      entitiesHtml
        ? `
      <div class="entity-section" style="margin-top:14px;">
        <p class="eyebrow">KEY TOPICS &amp; PROJECTS</p>
        <div class="entity-list">${entitiesHtml}</div>
      </div>
    `
        : ""
    }

    <div class="minutes">${formattedMinutes}</div>

    <div class="transcript-section" style="margin-top:18px; border-top:1px solid var(--rule); padding-top:12px;">
      <button type="button" class="btn-outline js-meeting-transcript" id="btn-transcript"
              style="font-size:0.8rem; padding:5px 12px; border-radius:4px;" data-meeting="${escapeHtml(m.id)}">
        Show full transcript
      </button>
      <div id="transcript-body" hidden
           style="margin-top:12px; max-height:520px; overflow-y:auto; padding-right:8px;"></div>
    </div>
  `;

}

function promptDelete(meetingId, actionType, title) {
  const modal = $("delete-confirm-modal");
  $("delete-meeting-id").value = meetingId;
  $("delete-action-type").value = actionType;

  if (actionType === "audio") {
    $("delete-modal-title").textContent = "Delete Local Audio File";
    $("delete-modal-sub").textContent = `Meeting: ${title}`;
    $("delete-warning-text").textContent =
      "This will delete the local audio recording from disk to free space. Your compiled minutes, speaker notes, and AI search indexing will remain intact.";
    $("btn-confirm-delete").textContent = "Delete Local Audio";
  } else {
    $("delete-modal-title").textContent = "Delete Entire Meeting & Brief";
    $("delete-modal-sub").textContent = `Meeting: ${title}`;
    $("delete-warning-text").textContent =
      "WARNING: This will permanently delete this meeting, its minutes, transcript, local audio, and AI knowledge graph entries. This cannot be undone.";
    $("btn-confirm-delete").textContent = "Permanently Delete";
  }
  modal.showModal();
}

async function executePendingDelete() {
  const meetingId = $("delete-meeting-id").value;
  const actionType = $("delete-action-type").value;
  const modal = $("delete-confirm-modal");
  const btn = $("btn-confirm-delete");
  if (!meetingId) return;

  btn.disabled = true;
  btn.textContent = "Deleting...";
  try {
    const res =
      actionType === "audio"
        ? await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/delete-audio`, { method: "POST" })
        : await fetch(`/api/meetings/${encodeURIComponent(meetingId)}`, { method: "DELETE" });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Deletion failed");
    }
    modal.close();
    showToast(
      actionType === "audio" ? "Audio file deleted (brief preserved)" : "Meeting permanently deleted",
      "success",
    );
    if (actionType === "all") state.activeMeetingId = null;
    await loadMeetings();
    await loadOverview();
  } catch (err) {
    alert(`Could not complete deletion: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

async function updateMeetingCategory(meetingId, domain, control = $("meeting-category")) {
  // A <select> cannot borrow withBusy's label swap, but it can still refuse a
  // second change until the write and the reload have both landed.
  if (control) control.disabled = true;
  try {
    const res = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/category`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        domain,
        type: domain === "Personal" ? "Personal & Household" : "General",
      }),
    });
    if (!res.ok) throw new Error("Could not update category");
    showToast(`Category updated to ${domain}`, "success");
    await loadMeetings();
  } catch (err) {
    showToast(err.message, "warn");
  } finally {
    // The detail pane may have been re-rendered by loadMeetings, in which case
    // this element is detached and re-enabling it is harmless.
    if (control) control.disabled = false;
  }
}

// ── Markdown Parser for Executive Briefs ──────────────────────────────
function renderMarkdown(md) {
  if (!md) return "";
  
  // Strip YAML frontmatter if present
  let text = md;
  if (text.startsWith("---")) {
    const parts = text.split("---", 3);
    if (parts.length >= 3) {
      text = parts.slice(2).join("---").trim();
    }
  }

  // Escape HTML characters
  let html = escapeHtml(text);

  // Headers (h3, h4)
  html = html.replace(/^### (.*$)/gim, '<h4>$1</h4>');
  html = html.replace(/^## (.*$)/gim, '<h3>$1</h3>');
  html = html.replace(/^# (.*$)/gim, '<h2>$1</h2>');

  // Bold & Italic
  html = html.replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>');
  html = html.replace(/\*(.*?)\*/gim, '<em>$1</em>');

  // Checkboxes / Action Items
  html = html.replace(/^- \[ \] (.*$)/gim, '<div class="action-item unchecked"><span class="check-box">☐</span> $1</div>');
  html = html.replace(/^- \[x\] (.*$)/gim, '<div class="action-item checked"><span class="check-box">☑</span> <s>$1</s></div>');

  // Bullet items
  html = html.replace(/^\- (.*$)/gim, '<li>$1</li>');
  html = html.replace(/^\* (.*$)/gim, '<li>$1</li>');

  // Wrap consecutive list items in <ul>
  html = html.replace(/(<li>.*<\/li>\s*)+/gim, '<ul>$&</ul>');

  // Paragraph line breaks
  html = html.replace(/\n\n/g, '<br/><br/>');

  return html;
}

// ── People & Contact Management ──────────────────────────────────────
async function loadPeople() {
  try {
    const res = await fetch("/api/people");
    if (!res.ok) return;
    const data = await res.json();
    state.people = data.people || [];
    const currentNames = new Set(state.people.map((person) => person.canonical));
    state.selectedPeople = new Set(
      [...state.selectedPeople].filter((name) => currentNames.has(name)),
    );
    renderPeopleList(state.people);
    updateDatalistSuggestions(state.people);
    loadPeopleSuggestions();
  } catch (err) {
    console.error("Failed to load people:", err);
  }
}

function renderPeopleList(people) {
  const list = $("people-list");
  if (!list) return;
  const term = ($("people-search")?.value || "").trim().toLowerCase();
  const visible = people.filter((person) => {
    const aliases = Array.isArray(person.aliases) ? person.aliases : [person.aliases || ""];
    return [person.canonical, person.role || "", ...aliases]
      .join(" ")
      .toLowerCase()
      .includes(term);
  });
  if (!visible.length) {
    list.innerHTML = `<p class="empty-cell">${term ? "No contacts match your search." : "No contacts added yet."}</p>`;
    updatePeopleMergeToolbar();
    return;
  }
  list.innerHTML = visible
    .map(
      (p) => {
        const aliases = Array.isArray(p.aliases)
          ? p.aliases
          : String(p.aliases || "").split(",").map((alias) => alias.trim()).filter(Boolean);
        const meetingCount = p.meetings || p.meeting_count || 0;
        const initials = p.canonical
          .split(/\s+/)
          .slice(0, 2)
          .map((part) => part[0] || "")
          .join("")
          .toUpperCase();
        return `
    <article class="person-row${state.selectedPeople.has(p.canonical) ? " selected" : ""}">
      <label class="person-select" title="Select ${escapeHtml(p.canonical)} to merge">
        <input type="checkbox" class="js-person-select" value="${escapeHtml(p.canonical)}"
          aria-label="Select ${escapeHtml(p.canonical)} to merge" ${state.selectedPeople.has(p.canonical) ? "checked" : ""} />
      </label>
      <span class="person-avatar" aria-hidden="true">${escapeHtml(initials)}</span>
      <div class="person-summary">
        <strong>${escapeHtml(p.canonical)}</strong>
        <span>${escapeHtml(p.role || "No role saved")} · ${meetingCount} meeting${meetingCount === 1 ? "" : "s"}</span>
        ${aliases.length ? `<small>Also known as ${aliases.map(escapeHtml).join(", ")}</small>` : ""}
      </div>
      <div class="person-actions">
        <button type="button" class="btn-text btn-sm js-rename-person" data-name="${escapeHtml(p.canonical)}">Fix spelling</button>
      </div>
    </article>
  `;
      }
    )
    .join("");
  updatePeopleMergeToolbar();
}

function updatePeopleMergeToolbar() {
  const count = state.selectedPeople.size;
  const names = [...state.selectedPeople].sort((a, b) => a.localeCompare(b));
  const label = $("people-selected-count");
  const merge = $("people-merge-selected");
  const clear = $("people-clear-selection");
  if (!label || !merge || !clear) return;
  label.textContent = count
    ? `${count} name${count === 1 ? "" : "s"} selected: ${names.slice(0, 3).join(", ")}${count > 3 ? ` +${count - 3} more` : ""}`
    : "Select names that belong to the same person";
  merge.disabled = count < 2;
  clear.hidden = count === 0;
}

function clearPeopleSelection() {
  state.selectedPeople.clear();
  renderPeopleList(state.people);
}

function updateDatalistSuggestions(people) {
  const datalist = $("canonical-suggestions");
  if (!datalist) return;
  datalist.innerHTML = people
    .map((p) => `<option value="${escapeHtml(p.canonical)}">${escapeHtml(p.role ? `${p.canonical} (${p.role})` : p.canonical)}</option>`)
    .join("");
}

async function loadPeopleSuggestions() {
  try {
    const res = await fetch("/api/people/suggestions");
    if (!res.ok) return;
    const data = await res.json();
    state.peopleSuggestions = data.suggestions || [];
    renderPeopleSuggestion();
  } catch (err) {
    console.error("Failed to load contact suggestions:", err);
  }
}

function renderPeopleSuggestion() {
  const panel = $("people-suggestions");
  const empty = $("people-suggestions-empty");
  const suggestion = state.peopleSuggestions[0];
  state.peopleSuggestionMergePreview = null;
  if (!suggestion) {
    panel.hidden = true;
    if (empty) empty.hidden = false;
    updateSpeakerResolutionSummary();
    return;
  }
  if (empty) empty.hidden = true;
  $("people-suggestion-yes").disabled = false;
  $("people-suggestion-rename").disabled = false;
  $("people-suggestion-no").disabled = false;
  const names = Array.isArray(suggestion.names) ? suggestion.names : [suggestion.source, suggestion.target];
  panel.dataset.names = JSON.stringify(names);
  panel.dataset.target = suggestion.target;
  $("people-suggestion-question").textContent =
    `Do these ${names.length} names describe the same person? ${names.map((name) => `“${name}”`).join(", ")}`;
  $("people-suggestion-result").textContent =
    `Yes keeps “${suggestion.target}” and folds the other spelling${names.length === 2 ? "" : "s"} into it.`;
  panel.hidden = false;
  $("people-suggestion-rename-panel").hidden = true;
  $("people-suggestion-preview-panel").hidden = true;
  $("people-suggestion-target").value = suggestion.target;
  $("people-suggestion-confirm").disabled = true;
  updateSpeakerResolutionSummary();
}

function revealPeopleSuggestionRename() {
  const panel = $("people-suggestions");
  const target = $("people-suggestion-target");
  target.value = panel.dataset.target || "";
  $("people-suggestion-rename-panel").hidden = false;
  $("people-suggestion-preview-panel").hidden = true;
  $("people-suggestion-confirm").disabled = true;
  target.focus();
}

const PEOPLE_SUGGESTION_CONTROL_IDS = [
  "people-suggestion-yes",
  "people-suggestion-rename",
  "people-suggestion-no",
  "people-suggestion-target",
  "people-suggestion-review",
  "people-suggestion-confirm",
];

function setPeopleSuggestionBusy(busy) {
  state.peopleSuggestionMergeBusy = busy;
  PEOPLE_SUGGESTION_CONTROL_IDS.forEach((id) => {
    const control = $(id);
    if (control) control.disabled = busy;
  });
  if (!busy && $("people-suggestion-confirm")) {
    $("people-suggestion-confirm").disabled = !state.peopleSuggestionMergePreview;
  }
}

function invalidatePeopleSuggestionPreview() {
  state.peopleSuggestionMergePreview = null;
  $("people-suggestion-preview-panel").hidden = true;
  $("people-suggestion-confirm").disabled = true;
}

function mergePreviewText(preview, orderNote = "") {
  const missing = (preview.missing_files || []).length;
  const conflicts = (preview.conflicts || []).length;
  const retained = preview.actual_target === preview.requested_target
    ? `Retain “${preview.actual_target}”.`
    : `Requested “${preview.requested_target}”; the hidden redirect retains “${preview.actual_target}”.`;
  return [
    retained,
    `${preview.affected_meetings} affected meeting(s); ${preview.files_changed} file(s); ${preview.literal_matches} literal match(es).`,
    `${missing} missing file(s); ${conflicts} warning(s).`,
    "Folded spellings become hidden redirects, not visible aliases.",
    orderNote,
  ].filter(Boolean).join(" ");
}

async function requestPeopleMergePreview(names, into) {
  const res = await fetch("/api/people/merge-preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names, into }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Could not preview this merge");
  return data;
}

async function previewPeopleSuggestion() {
  if (state.peopleSuggestionMergeBusy) return false;
  const panel = $("people-suggestions");
  const names = JSON.parse(panel.dataset.names || "[]");
  const into = $("people-suggestion-target").value.trim() || panel.dataset.target;
  if (names.length < 2 || !into) return false;
  setPeopleSuggestionBusy(true);
  try {
    const preview = await requestPeopleMergePreview(names, into);
    state.peopleSuggestionMergePreview = preview;
    $("people-suggestion-preview").textContent = mergePreviewText(preview);
    $("people-suggestion-preview-panel").hidden = false;
    return true;
  } catch (err) {
    state.peopleSuggestionMergePreview = null;
    showToast(err.message, "error");
    return false;
  } finally {
    setPeopleSuggestionBusy(false);
  }
}

function mergeResultText(data, sourceCount) {
  const outstanding = Number(data.minutes_missing || 0)
    + Number(data.rewrite_conflicts || 0)
    + Number(data.pending_rewrites || 0);
  const indexState = outstanding
    ? "Resolve the outstanding minutes before re-indexing."
    : "Corrected minutes are ready to re-index.";
  return [
    `Merged ${sourceCount} spelling(s) into “${data.target}”.`,
    `${data.minutes_rewritten || 0} minutes rewritten; ${data.minutes_unchanged || 0} already correct;`,
    `${data.minutes_missing || 0} missing; ${data.rewrite_conflicts || 0} conflicting; ${data.pending_rewrites || 0} pending.`,
    indexState,
  ].join(" ");
}

async function postPeopleMerge(names, into, expectedDigest) {
  const res = await fetch("/api/people/merge-many", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names, into, expected_digest: expectedDigest }),
  });
  const data = await res.json();
  return { res, data };
}

async function confirmPeopleSuggestionMerge() {
  const preview = state.peopleSuggestionMergePreview;
  if (state.peopleSuggestionMergeBusy || !preview) return false;
  const panel = $("people-suggestions");
  const names = JSON.parse(panel.dataset.names || "[]");
  setPeopleSuggestionBusy(true);
  try {
    const { res, data } = await postPeopleMerge(
      names,
      preview.requested_target,
      preview.digest,
    );
    if (res.status === 409) {
      state.peopleSuggestionMergePreview = null;
      $("people-suggestion-preview").textContent =
        "This preview is stale. Review the merge again before confirming.";
      showToast(data.error || "The merge preview changed", "error");
      return false;
    }
    if (!res.ok) throw new Error(data.error || "Failed to combine names");
    state.peopleSuggestions.shift();
    state.peopleSuggestionMergePreview = null;
    state.selectedPeople.clear();
    renderPeopleSuggestion();
    refreshPeopleDependentViews();
    showToast(mergeResultText(data, names.length), "success");
    return true;
  } catch (err) {
    showToast(err.message, "error");
    return false;
  } finally {
    setPeopleSuggestionBusy(false);
  }
}

function updateSpeakerResolutionSummary() {
  const voiceTotal = (state.voiceClusters || []).length + (state.unresolvedSpeakers || []).length;
  const nameTotal = (state.peopleSuggestions || []).length;
  const total = voiceTotal + nameTotal;
  const count = $("speaker-resolution-count");
  const summary = $("speaker-resolution-summary");
  if (count) {
    count.textContent = total ? String(total) : "";
    count.hidden = total === 0;
  }
  if (summary) {
    const parts = [];
    if (voiceTotal) parts.push(`${voiceTotal} voice${voiceTotal === 1 ? "" : "s"}`);
    if (nameTotal) parts.push(`${nameTotal} possible duplicate name${nameTotal === 1 ? "" : "s"}`);
    summary.textContent = total ? `${parts.join(" and ")} waiting` : "All current speakers are resolved.";
  }
  return { voiceTotal, nameTotal, total };
}

async function acceptPeopleSuggestion() {
  const panel = $("people-suggestions");
  $("people-suggestion-target").value = panel.dataset.target || "";
  $("people-suggestion-rename-panel").hidden = true;
  return previewPeopleSuggestion();
}

async function dismissPeopleSuggestion() {
  if (state.peopleSuggestionMergeBusy) return false;
  const panel = $("people-suggestions");
  setPeopleSuggestionBusy(true);
  try {
    const res = await fetch("/api/people/suggestions/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: JSON.parse(panel.dataset.names || "[]") }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not dismiss this suggestion");
    state.peopleSuggestions.shift();
    renderPeopleSuggestion();
    return true;
  } catch (err) {
    showToast(err.message, "error");
    return false;
  } finally {
    setPeopleSuggestionBusy(false);
  }
}

function openSelectedPeopleMergeModal() {
  const selected = [...state.selectedPeople].sort((a, b) => a.localeCompare(b));
  if (selected.length < 2) return;
  $("person-merge-sources").value = JSON.stringify(selected);
  $("person-merge-selected-list").innerHTML = selected
    .map((name) => `<span>${escapeHtml(name)}</span>`)
    .join("");
  $("person-merge-target").innerHTML = [
    '<option value="">Choose a spelling…</option>',
    ...selected.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`),
  ].join("");
  $("person-merge-target").value = "";
  $("person-merge-target-custom").value = "";
  state.personMergePreview = null;
  $("person-merge-preview").textContent =
    "Nothing changes until you review the exact impact. Selected names are considered in alphabetical order.";
  $("person-merge-save").disabled = true;
  $("person-merge-save").textContent = "Review Merge";
  $("person-merge-modal").showModal();
  setTimeout(() => $("person-merge-target").focus(), 50);
}

function updatePersonMergePreview() {
  state.personMergePreview = null;
  const target = personMergeTarget();
  $("person-merge-preview").textContent = target
    ? `Review the exact impact of retaining “${target}”. Folded spellings will become hidden redirects, not visible aliases.`
    : "Nothing changes until you review the exact impact.";
  $("person-merge-save").disabled = !target;
  $("person-merge-save").textContent = "Review Merge";
}

function personMergeTarget() {
  return $("person-merge-target-custom").value.trim()
    || $("person-merge-target").value.trim();
}

const PERSON_MERGE_CONTROL_IDS = [
  "person-merge-target",
  "person-merge-target-custom",
  "person-merge-save",
  "person-merge-cancel",
];

function setPersonMergeBusy(busy) {
  state.personMergeBusy = busy;
  PERSON_MERGE_CONTROL_IDS.forEach((id) => {
    const control = $(id);
    if (control) control.disabled = busy;
  });
  if (!busy && $("person-merge-save")) {
    $("person-merge-save").disabled = !personMergeTarget();
  }
}

function closePersonMergeModal() {
  $("person-merge-modal").close();
}

function openPersonRenameModal(name) {
  $("person-rename-source").value = name;
  $("person-rename-name").value = name;
  state.personRenamePreview = null;
  $("person-rename-preview").textContent =
    "Nothing changes until you review the exact impact.";
  $("person-rename-save").textContent = "Review Spelling";
  $("person-rename-modal").showModal();
  setTimeout(() => {
    $("person-rename-name").focus();
    $("person-rename-name").select();
  }, 50);
}

function closePersonRenameModal() {
  $("person-rename-modal").close();
}

function invalidatePersonRenamePreview() {
  state.personRenamePreview = null;
  $("person-rename-preview").textContent =
    "Review the exact impact before changing this spelling.";
  $("person-rename-save").textContent = "Review Spelling";
}

const PERSON_RENAME_CONTROL_IDS = [
  "person-rename-name",
  "person-rename-save",
  "person-rename-cancel",
];

function setPersonRenameBusy(busy) {
  state.personRenameBusy = busy;
  PERSON_RENAME_CONTROL_IDS.forEach((id) => {
    const control = $(id);
    if (control) control.disabled = busy;
  });
}

async function submitAddPerson(trigger = triggerButton()) {
  const canonical = $("person-canonical").value.trim();
  const role = $("person-role").value.trim() || null;
  const aliasesRaw = $("person-aliases").value.trim();
  const aliases = aliasesRaw ? aliasesRaw.split(",").map((value) => value.trim()).filter(Boolean) : null;
  if (!canonical) return;
  await withBusy(trigger, "Saving\u2026", async () => {
    try {
      const res = await fetch("/api/people", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical, role, aliases }),
      });
      if (!res.ok) throw new Error("Failed to save person");
      showToast(`Saved contact: ${canonical}`, "success");
      $("person-canonical").value = "";
      $("person-role").value = "";
      $("person-aliases").value = "";
      $("form-add-person").closest("details").open = false;
      await Promise.all([loadPeople(), loadOverview()]);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

async function submitMergePerson() {
  const names = JSON.parse($("person-merge-sources").value || "[]");
  const into = personMergeTarget();
  if (state.personMergeBusy || names.length < 2 || !into) return false;
  const save = $("person-merge-save");
  setPersonMergeBusy(true);
  try {
    if (!state.personMergePreview) {
      const preview = await requestPeopleMergePreview(names, into);
      state.personMergePreview = preview;
      $("person-merge-preview").textContent = mergePreviewText(
        preview,
        "Selected names are considered in alphabetical order for role inheritance.",
      );
      save.textContent = "Confirm Merge";
      return true;
    }

    const { res, data } = await postPeopleMerge(
      names,
      state.personMergePreview.requested_target,
      state.personMergePreview.digest,
    );
    if (res.status === 409) {
      state.personMergePreview = null;
      $("person-merge-preview").textContent =
        "This preview is stale. Refresh the preview before confirming.";
      save.textContent = "Refresh Preview";
      showToast(data.error || "The merge preview changed", "error");
      return false;
    }
    if (!res.ok) throw new Error(data.error || "Failed to combine names");
    state.personMergePreview = null;
    state.selectedPeople.clear();
    closePersonMergeModal();
    refreshPeopleDependentViews();
    showToast(mergeResultText(data, names.length), "success");
    return true;
  } catch (err) {
    showToast(err.message, "error");
    return false;
  } finally {
    setPersonMergeBusy(false);
    if (state.personMergePreview) save.textContent = "Confirm Merge";
  }
}

function refreshPeopleDependentViews() {
  loadPeople();
  loadVoiceClusters();
  loadMeetings();
  loadOverview();
  if (state.activeMeetingId) selectMeeting(state.activeMeetingId);
}

async function submitPersonRename() {
  const fromName = $("person-rename-source").value.trim();
  const newName = $("person-rename-name").value.trim();
  if (
    state.personRenameBusy
    || !fromName
    || !newName
    || fromName === newName
  ) return false;
  const save = $("person-rename-save");
  setPersonRenameBusy(true);
  try {
    if (!state.personRenamePreview) {
      const preview = await requestPeopleMergePreview([fromName], newName);
      state.personRenamePreview = preview;
      $("person-rename-preview").textContent = mergePreviewText(preview);
      save.textContent = "Confirm Spelling";
      return true;
    }
    const res = await fetch("/api/people/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from_name: fromName,
        new_name: state.personRenamePreview.requested_target,
        expected_digest: state.personRenamePreview.digest,
      }),
    });
    const data = await res.json();
    if (res.status === 409) {
      state.personRenamePreview = null;
      $("person-rename-preview").textContent =
        "This preview is stale. Refresh the preview before confirming.";
      save.textContent = "Refresh Preview";
      showToast(data.error || "The spelling preview changed", "error");
      return false;
    }
    if (!res.ok) throw new Error(data.error || "Could not change this spelling");
    state.personRenamePreview = null;
    showToast(mergeResultText(data, 1), "success");
    closePersonRenameModal();
    refreshPeopleDependentViews();
    return true;
  } catch (err) {
    showToast(err.message, "error");
    return false;
  } finally {
    setPersonRenameBusy(false);
    if (state.personRenamePreview) save.textContent = "Confirm Spelling";
  }
}

function openSpeakerModal(meetingId, label, currentName) {
  $("modal-meeting-id").value = meetingId;
  if (!label) label = "SPEAKER_00";
  $("modal-speaker-label").value = label;
  $("modal-speaker-name").value = currentName || "";
  $("speaker-modal-sub").textContent = `Assign contact name to speaker tag: ${label}`;

  const audioEl = $("modal-snippet-audio");
  const statusEl = $("modal-snippet-status");
  const transcriptEl = $("modal-snippet-transcript");
  if (audioEl) {
    statusEl.textContent = "Loading voice clip...";
    transcriptEl.textContent = "";
    const durable =
      `/api/voices/snippet?meeting_id=${encodeURIComponent(meetingId)}` +
      `&label=${encodeURIComponent(label)}&index=0`;
    const liveCut =
      `/api/audio/snippet?meeting_id=${encodeURIComponent(meetingId)}` +
      `&label=${encodeURIComponent(label)}&duration=20`;
    let usedFallback = false;
    audioEl.src = durable;
    audioEl.oncanplay = () => {
      statusEl.textContent = "Ready to play";
      audioEl.play().catch(() => {});
    };
    audioEl.onerror = () => {
      if (!usedFallback) {
        usedFallback = true;
        statusEl.textContent = "No retained clip; cutting from the recording...";
        audioEl.src = liveCut;
        return;
      }
      statusEl.textContent = "Audio sample unavailable";
    };
    audioEl.load();
  }
  $("speaker-modal").showModal();
  setTimeout(() => $("modal-speaker-name").focus(), 50);
}

function closeSpeakerModal() {
  const audioEl = $("modal-snippet-audio");
  if (audioEl) {
    audioEl.pause();
    audioEl.src = "";
  }
  $("speaker-modal").close();
}

async function submitSpeakerModal(trigger = triggerButton()) {
  const meetingId = $("modal-meeting-id").value;
  const label = $("modal-speaker-label").value;
  const name = $("modal-speaker-name").value.trim();
  await withBusy(trigger, "Saving\u2026", async () => {
    try {
      const res = await fetch(`/api/meetings/${meetingId}/speakers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, name, confidence: "confirmed" }),
      });
      if (!res.ok) throw new Error("Failed to update speaker");
      showToast(`Saved speaker: ${label} → ${name}`, "success");
      closeSpeakerModal();
      selectMeeting(meetingId);
      await Promise.all([loadVoiceClusters(), loadMeetings(), loadPeople(), loadOverview()]);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

async function confirmOneOffSpeaker(meetingId, label, name, trigger = triggerButton()) {
  if (!meetingId || !label || !name) return;
  await withBusy(trigger, "Saving\u2026", async () => {
    try {
      const res = await fetch(`/api/meetings/${meetingId}/speakers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, name, confidence: "confirmed" }),
      });
      if (!res.ok) throw new Error("Failed to assign this speaker");
      showToast(`Assigned ${label} as ${name}.`, "success");
      await Promise.all([loadVoiceClusters(), loadMeetings(), loadPeople(), loadOverview()]);
      if (state.activeMeetingId === meetingId) selectMeeting(meetingId);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

// ── Commitment Register (Open Commitments by Owner) ────────────────────
// The commitment register has no markup of its own in index.html - built here
// at runtime the same way setupAskPanel() builds the "New conversation"
// button, so the pane this belongs to (People & Team) never needed a
// hand-authored placeholder for a feature that did not exist yet.
async function loadCommitments() {
  try {
    const res = await fetch("/api/commitments");
    if (!res.ok) return;
    const data = await res.json();
    renderCommitmentsPanel(data.commitments || []);
  } catch (err) {
    console.error("Failed to load commitments:", err);
  }
}

function setupCommitmentsPanel() {
  const column = document.querySelector("#tab-knowledge .knowledge-column");
  if (!column || $("commitments-tbody")) return; // no panel on this page, or already set up

  const card = document.createElement("div");
  card.className = "section-card";
  card.innerHTML = `
    <h3>Open Commitments</h3>
    <p class="card-desc">Action items extracted from your minutes, grouped by owner. Past-due items are flagged.</p>
    <div class="table-scroll">
      <table class="data-table" id="commitments-table">
        <thead>
          <tr>
            <th>Owner</th>
            <th>Commitment</th>
            <th>Due</th>
          </tr>
        </thead>
        <tbody id="commitments-tbody">
          <tr><td colspan="3" class="empty-cell">Loading commitments...</td></tr>
        </tbody>
      </table>
    </div>
  `;
  column.appendChild(card);
}

function renderCommitmentsPanel(commitments) {
  const tbody = $("commitments-tbody");
  if (!tbody) return;

  const open = commitments.filter((c) => c.state === "open");
  if (!open.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-cell">No open commitments recorded yet.</td></tr>`;
    return;
  }

  // Grouped by owner, busiest owner first; unattributed commitments trail.
  const byOwner = new Map();
  for (const c of open) {
    const key = c.owner || "Unassigned";
    if (!byOwner.has(key)) byOwner.set(key, []);
    byOwner.get(key).push(c);
  }
  const owners = [...byOwner.keys()].sort((a, b) => {
    if (a === "Unassigned") return 1;
    if (b === "Unassigned") return -1;
    return byOwner.get(b).length - byOwner.get(a).length;
  });

  tbody.innerHTML = owners
    .map((owner) => {
      const items = byOwner.get(owner);
      return items
        .map(
          (c, i) => `
        <tr>
          ${i === 0 ? `<td rowspan="${items.length}"><strong>${escapeHtml(owner)}</strong></td>` : ""}
          <td>${escapeHtml(c.text)}${c.overdue ? ` <span class="badge warn">Overdue</span>` : ""}</td>
          <td>${escapeHtml(c.due_date || "unspecified")}</td>
        </tr>
      `
        )
        .join("");
    })
    .join("");
}

async function askArchive() {
  const question = $("question").value.trim();
  const mode = $("query-mode").value;
  const answerEl = $("answer");

  if (!question) {
    showToast("Please enter a question.", "info");
    return;
  }

  // This is the slowest action in the dashboard - retrieval plus an LLM call,
  // which can run well past a minute on a broad synthesis. A static "preparing
  // answer" line cannot be told apart from a hung request, so the seconds are
  // counted on screen: a number that keeps moving is the proof of life.
  const submit = $("query-form").querySelector('button[type="submit"]');
  answerEl.className = "answer";
  answerEl.innerHTML =
    '<p>Consulting your meeting archive and preparing answer' +
    ' <span id="answer-elapsed" class="answer-elapsed">0s</span></p>';
  let elapsed = 0;
  const ticker = setInterval(() => {
    elapsed += 1;
    const readout = $("answer-elapsed");
    if (readout) readout.textContent = `${elapsed}s`;
  }, 1000);

  await withBusy(submit, null, async () => {
    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, mode, session_id: getChatSessionId() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Query could not be completed");
      if (data.session_id) setChatSessionId(data.session_id);

      const retrievedSec = Number.isFinite(data.retrieval_sec) ? data.retrieval_sec.toFixed(1) : "?";
      const providerNote = data.provider ? ` · Answered by: ${escapeHtml(data.provider)}` : "";
      answerEl.innerHTML = `
        <div>${renderMarkdown(data.answer)}</div>
        <span class="answer-meta">
          Retrieved in ${retrievedSec}s · Mode: ${escapeHtml(mode)}${providerNote}
        </span>
      `;
    } catch (err) {
      answerEl.className = "answer empty";
      answerEl.innerHTML = `<p style="color:var(--clay)">Search service note: ${escapeHtml(err.message)}</p>`;
    } finally {
      clearInterval(ticker);
    }
  });
}

// ── Utilities & Formatting ───────────────────────────────────────────
function showToast(message, type = "info") {
  const container = $("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 250);
  }, 3500);
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatDuration(sec) {
  if (!sec || isNaN(sec)) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}m ${s}s`;
}

function formatSeconds(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}

// ── Speaker resolution queue ─────────────────────────────────────────
async function loadVoiceClusters() {
  try {
    const res = await fetch("/api/speakers/queue");
    if (!res.ok) return;
    const data = await res.json();
    state.voiceClusters = data.clusters || [];
    state.unresolvedSpeakers = data.one_offs || [];
    applySpeakerControls();
  } catch (err) {
    console.error("Failed to load speaker resolution queue:", err);
  }
}

function renderVoiceCards(clusters, oneOffRows) {
  stopClusterPlayback();
  const banner = $("voice-review-banner");
  const clusterList = $("speaker-resolution-clusters");
  const oneOffs = oneOffRows || state.unresolvedSpeakers || [];
  const oneOffList = $("speaker-resolution-oneoffs");
  // `total` counts every waiting decision, filtered or not - the tab badge has
  // to keep meaning "work outstanding". Whether THIS view came back empty is a
  // different question, and the two answers need different words.
  const { total } = updateSpeakerResolutionSummary();
  const filteredOut = Boolean(total) && !clusters.length && !oneOffs.length;

  if (!total) {
    if (banner) banner.style.display = "none";
    if (clusterList) clusterList.innerHTML = '<p class="empty-cell">No recurring voices need a decision.</p>';
    if (oneOffList) oneOffList.innerHTML = '<p class="empty-cell">No one-off unnamed speakers.</p>';
    return;
  }

  const cards = clusters
    .map((c) => {
      const bestMatchName = c.best_canonical;
      const matchConfidence = c.best_score ? Math.round(c.best_score * 100) : null;
      const VOICE_SUGGEST_MIN = 0.7;
      const voiceCredible = bestMatchName && (c.best_score || 0) >= VOICE_SUGGEST_MIN;
      const matchText = !bestMatchName
        ? `Unrecognized recurring speaker`
        : voiceCredible
          ? `Best Match: <strong>${escapeHtml(bestMatchName)}</strong> <span class="similarity-pill">${matchConfidence}% confidence</span>`
          : `No confident voiceprint match <span class="voice-weak">closest: ${escapeHtml(bestMatchName)}, ${matchConfidence}%</span>`;

      const llmName = c.llm_suggestion;
      const transcriptSuggestion =
        llmName && llmName !== bestMatchName
          ? `<button type="button" class="btn-outline btn-sm js-voice-confirm" data-cluster="${escapeHtml(c.id)}" data-name="${escapeHtml(llmName)}">✓ Transcript suggests ${escapeHtml(llmName)}</button>`
          : "";

      const confirmBtn = voiceCredible
        ? `<button type="button" class="btn-action btn-sm js-voice-confirm" data-cluster="${escapeHtml(c.id)}" data-name="${escapeHtml(bestMatchName)}">✓ Confirm as ${escapeHtml(bestMatchName)}</button>`
        : "";

      // Say how much audio there is before offering to play it. Clips are cut
      // once at enrollment and the source audio is deleted right after
      // transcription, so a speaker has 18s, 6s or nothing - permanently. An
      // unlabelled "listen" makes a short speaker look broken instead of short.
      const clipSeconds = c.clip_seconds || 0;
      const listenBtn = clipSeconds
        ? `<button type="button" class="btn-outline btn-sm js-voice-listen" data-cluster="${escapeHtml(c.id)}" aria-expanded="false">▶ Listen (${clipSeconds}s)</button>`
        : `<span class="voice-noclip" title="The source audio was deleted after transcription, so no clip was retained.">No audio retained</span>`;

      return `
        <div class="voice-card">
          <div class="voice-card-header">
            <div class="voice-card-title">
              <span class="voice-icon">🎙️</span>
              <div>
                <strong>Recurring Voice Detected</strong>
                <p class="voice-meta">Heard across ${c.size} meeting${c.size > 1 ? "s" : ""} · ${formatDuration(c.total_speech)} total speech</p>
              </div>
            </div>
          </div>
          <div class="voice-card-body">
            <p class="voice-match-text">${matchText}</p>
            <div class="voice-members">
              ${(c.members || []).slice(0, 3).map((m) => `<span class="voice-member-chip">📅 ${escapeHtml(m.meeting_date || "Past Meeting")}: ${escapeHtml(m.meeting_title || "Recording")} (${m.label})</span>`).join("")}
            </div>
          </div>
          <div class="voice-player js-voice-player" hidden>
            <div class="voice-player-heading">
              <strong>Voice sample</strong>
              <span class="js-voice-clip-status" aria-live="polite">Ready</span>
            </div>
            <audio class="js-voice-audio" controls preload="none"></audio>
            <div class="voice-player-nav">
              <button type="button" class="btn-text btn-sm js-voice-previous" aria-label="Play previous voice clip">← Previous clip</button>
              <button type="button" class="btn-text btn-sm js-voice-next" aria-label="Play next voice clip">Next clip →</button>
            </div>
          </div>
          <div class="voice-card-actions">
            ${listenBtn}
            ${confirmBtn}
            ${transcriptSuggestion}
            <button type="button" class="btn-outline btn-sm js-voice-custom" data-cluster="${escapeHtml(c.id)}">Choose or add speaker</button>
            <button type="button" class="btn-text btn-sm js-voice-dismiss" data-cluster="${escapeHtml(c.id)}">Not a person / noise</button>
          </div>
        </div>
      `;
    });
  const cardsHtml = cards.join("");

  if (banner) {
    banner.innerHTML = `
      <div class="voice-banner-inner">
        <div class="voice-banner-header">
          <h3>🎙️ ${total} speaker decision${total === 1 ? "" : "s"} waiting</h3>
          <p>Open the dedicated resolver to listen, choose a person, and clean up similar names in one place.</p>
          <button type="button" class="btn-action btn-sm js-open-speaker-resolver">Resolve speakers</button>
        </div>
      </div>
    `;
    banner.style.display = "block";
  }

  if (clusterList) {
    clusterList.innerHTML =
      cardsHtml ||
      (filteredOut
        ? '<p class="empty-cell">No recurring voice matches these filters.</p>'
        : '<p class="empty-cell">No recurring voices need a decision.</p>');
  }
  if (oneOffList) {
    oneOffList.innerHTML = renderOneOffSpeakerCards(oneOffs, filteredOut);
  }
}

function renderOneOffSpeakerCards(oneOffs, filteredOut) {
  if (!oneOffs.length) {
    return filteredOut
      ? '<p class="empty-cell">No one-off label matches these filters.</p>'
      : '<p class="empty-cell">No one-off unnamed speakers.</p>';
  }
  return oneOffs.map((speaker) => {
    const voiceMatch = speaker.best_canonical && (speaker.best_score || 0) >= 0.7
      ? `<button type="button" class="btn-action btn-sm js-oneoff-confirm" data-meeting="${escapeHtml(speaker.meeting_id)}" data-label="${escapeHtml(speaker.label)}" data-name="${escapeHtml(speaker.best_canonical)}">Use ${escapeHtml(speaker.best_canonical)}</button>`
      : "";
    const transcriptMatch = speaker.llm_suggestion && speaker.llm_suggestion !== speaker.best_canonical
      ? `<button type="button" class="btn-outline btn-sm js-oneoff-confirm" data-meeting="${escapeHtml(speaker.meeting_id)}" data-label="${escapeHtml(speaker.label)}" data-name="${escapeHtml(speaker.llm_suggestion)}">Use ${escapeHtml(speaker.llm_suggestion)}</button>`
      : "";
    return `
      <article class="speaker-oneoff-card">
        <div>
          <strong>${escapeHtml(speaker.meeting_title || "Meeting")}</strong>
          <p>${escapeHtml(speaker.meeting_date || "Past meeting")} · ${escapeHtml(speaker.label)}${speaker.speech_sec ? ` · ${formatDuration(speaker.speech_sec)} of speech` : ""}</p>
        </div>
        <div class="speaker-oneoff-actions">
          <button type="button" class="btn-outline btn-sm js-oneoff-review" data-meeting="${escapeHtml(speaker.meeting_id)}" data-label="${escapeHtml(speaker.label)}">Listen &amp; choose name</button>
          ${voiceMatch}
          ${transcriptMatch}
        </div>
      </article>
    `;
  }).join("");
}

document.addEventListener("click", (event) => {
  const personRow = event.target.closest(".person-row");
  if (personRow && !event.target.closest("button, input, label, a")) {
    personRow.querySelector(".js-person-select")?.click();
    return;
  }
  const button = event.target.closest(".js-meeting-transcript");
  if (button) {
    toggleTranscript(button.dataset.meeting);
    return;
  }
  const speaker = event.target.closest(".js-speaker-chip");
  if (speaker) {
    openSpeakerModal(speaker.dataset.meeting, speaker.dataset.label, speaker.dataset.name);
    return;
  }
  const rename = event.target.closest(".js-rename-person");
  if (rename) {
    openPersonRenameModal(rename.dataset.name);
  }
});

document.addEventListener("change", (event) => {
  const checkbox = event.target.closest(".js-person-select");
  if (!checkbox) return;
  if (checkbox.checked) state.selectedPeople.add(checkbox.value);
  else state.selectedPeople.delete(checkbox.value);
  checkbox.closest(".person-row")?.classList.toggle("selected", checkbox.checked);
  updatePeopleMergeToolbar();
});

// Voice-card actions are bound here, not with inline onclick handlers.
//
// Speaker names reach this card from an LLM reading a meeting transcript and
// from the people registry - untrusted either way. `escapeHtml` is an
// HTML-entity encoder, and a browser HTML-decodes an attribute value BEFORE the
// JS parser sees it, so an escaped `&#039;` becomes a real quote inside an
// `onclick="...('name')"` string and closes it. A name of
//   x'),alert(1),confirmVoiceCluster('
// executed. Data attributes carry the name as data and `.dataset` reads it back
// without ever parsing it as code.
//
// Delegated from document so it survives every re-render of the card list.
document.addEventListener("click", (event) => {
  const openResolver = event.target.closest(".js-open-speaker-resolver");
  if (openResolver) {
    const speakersTab = $("tab-button-speakers");
    activateTab(speakersTab);
    requestAnimationFrame(() => $("speaker-resolution-heading")?.focus());
    return;
  }
  const openPeople = event.target.closest(".js-open-people-manager");
  if (openPeople) {
    const peopleTab = $("tab-button-knowledge");
    activateTab(peopleTab);
    requestAnimationFrame(() => $("recognized-contacts-heading")?.focus());
    return;
  }
  const listen = event.target.closest(".js-voice-listen");
  if (listen) {
    playClusterClips(listen.dataset.cluster, listen);
    return;
  }
  const previous = event.target.closest(".js-voice-previous");
  if (previous) {
    moveClusterClip(-1, previous);
    return;
  }
  const next = event.target.closest(".js-voice-next");
  if (next) {
    moveClusterClip(1, next);
    return;
  }
  const confirm = event.target.closest(".js-voice-confirm");
  if (confirm) {
    confirmVoiceCluster(confirm.dataset.cluster, confirm.dataset.name, confirm);
    return;
  }
  const dismiss = event.target.closest(".js-voice-dismiss");
  if (dismiss) {
    dismissVoiceCluster(dismiss.dataset.cluster, dismiss);
    return;
  }
  const custom = event.target.closest(".js-voice-custom");
  if (custom) openVoiceNameModal(custom.dataset.cluster);
  const oneOffReview = event.target.closest(".js-oneoff-review");
  if (oneOffReview) {
    openSpeakerModal(oneOffReview.dataset.meeting, oneOffReview.dataset.label, "");
    return;
  }
  const oneOffConfirm = event.target.closest(".js-oneoff-confirm");
  if (oneOffConfirm) {
    confirmOneOffSpeaker(
      oneOffConfirm.dataset.meeting,
      oneOffConfirm.dataset.label,
      oneOffConfirm.dataset.name,
      oneOffConfirm,
    );
  }
});

// Play every retained clip for a cluster back to back.
//
// One 6-second clip is not enough to recognise a voice, and the clips are
// separate files by construction: they are cut from moments at least 60s apart
// so three samples are three different sentences, not one sentence in thirds.
// Playing them in sequence is the closest thing to a continuous sample that the
// retained audio allows - the source recording is long gone.
let clusterPlayer = null;

function stopClusterPlayback() {
  if (!clusterPlayer) return;
  clusterPlayer.audio.pause();
  clusterPlayer.audio.onended = null;
  clusterPlayer.audio.onerror = null;
  clusterPlayer.trigger?.setAttribute("aria-expanded", "false");
  clusterPlayer.panel.hidden = true;
  clusterPlayer = null;
}

function clusterClipQueue(cluster) {
  const queue = [];
  for (const member of cluster.members || []) {
    for (let i = 0; i < (member.snippet_count || 0); i += 1) {
      queue.push(
        `/api/voices/snippet?meeting_id=${encodeURIComponent(member.meeting_id)}` +
          `&label=${encodeURIComponent(member.label)}&index=${i}`,
      );
    }
  }
  return queue;
}

function showClusterClip(index, autoplay = true) {
  if (!clusterPlayer) return;
  const { audio, queue, status, previous, next } = clusterPlayer;
  clusterPlayer.index = Math.max(0, Math.min(index, queue.length - 1));
  const displayIndex = clusterPlayer.index + 1;
  status.textContent = `Clip ${displayIndex} of ${queue.length}`;
  previous.disabled = clusterPlayer.index === 0;
  next.disabled = clusterPlayer.index === queue.length - 1;
  audio.src = queue[clusterPlayer.index];
  audio.load();
  if (autoplay) {
    audio.play().catch(() => {
      status.textContent = `Clip ${displayIndex} of ${queue.length} · Press play to start`;
    });
  }
}

function playClusterClips(clusterId, trigger) {
  const cluster = (state.voiceClusters || []).find((c) => c.id === clusterId);
  if (!cluster) return;

  const queue = clusterClipQueue(cluster);
  if (!queue.length) {
    showToast("No clip was retained for this voice.", "error");
    return;
  }

  const card = trigger.closest(".voice-card");
  const panel = card?.querySelector(".js-voice-player");
  const audio = panel?.querySelector(".js-voice-audio");
  if (!panel || !audio) return;

  // Stop whatever was playing first. Two cards playing at once is worse than
  // useless for telling two voices apart.
  stopClusterPlayback();
  panel.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  clusterPlayer = {
    audio,
    queue,
    index: 0,
    panel,
    trigger,
    status: panel.querySelector(".js-voice-clip-status"),
    previous: panel.querySelector(".js-voice-previous"),
    next: panel.querySelector(".js-voice-next"),
  };
  audio.onended = () => {
    if (!clusterPlayer || clusterPlayer.audio !== audio) return;
    if (clusterPlayer.index < clusterPlayer.queue.length - 1) {
      showClusterClip(clusterPlayer.index + 1);
    } else {
      clusterPlayer.status.textContent = `Finished · ${clusterPlayer.queue.length} clip${clusterPlayer.queue.length === 1 ? "" : "s"}`;
    }
  };
  // A missing file must not stall the queue - skip to the next clip.
  audio.onerror = () => {
    if (!clusterPlayer || clusterPlayer.audio !== audio) return;
    if (clusterPlayer.index < clusterPlayer.queue.length - 1) {
      showClusterClip(clusterPlayer.index + 1);
    } else {
      clusterPlayer.status.textContent = "Voice sample unavailable";
    }
  };
  showClusterClip(0);
}

function moveClusterClip(offset, control) {
  if (!clusterPlayer || !clusterPlayer.panel.contains(control)) return;
  showClusterClip(clusterPlayer.index + offset);
}

async function confirmConfidentVoiceClusters(trigger = triggerButton()) {
  const candidates = (state.voiceClusters || []).filter(
    (cluster) => cluster.best_canonical && (cluster.best_score || 0) >= 0.85,
  );
  if (!candidates.length) {
    showToast("No cluster is confident enough to accept automatically.", "error");
    return;
  }
  const names = [...new Set(candidates.map((cluster) => cluster.best_canonical))].join(", ");
  if (!window.confirm(`Confirm ${candidates.length} voice(s) as: ${names}?`)) return;
  await withBusy(trigger, "Confirming\u2026", async () => {
    try {
      const res = await fetch("/api/voices/confirm-confident", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threshold: 0.85 }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to confirm voices");
      showToast(
        `Enrolled ${data.clusters} voice(s) across ${data.meetings} meeting(s). ${data.skipped} left for review.`,
        "success",
      );
      await Promise.all([loadVoiceClusters(), loadMeetings(), loadPeople(), loadOverview()]);
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

// `trigger` is null when the modal calls this, because submitVoiceNameModal
// already owns the pending state of its own Save button.
async function confirmVoiceCluster(clusterId, canonical, trigger = null) {
  const outcome = await withBusy(trigger, "Confirming\u2026", async () => {
    try {
      const res = await fetch("/api/voices/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cluster_id: clusterId, canonical }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to confirm voice");
      showToast(`Enrolled voiceprint for '${canonical}' across ${data.confirmed || "all"} meetings!`, "success");
      await Promise.all([loadVoiceClusters(), loadMeetings(), loadPeople(), loadOverview()]);
      if (state.activeMeetingId) selectMeeting(state.activeMeetingId);
      return true;
    } catch (err) {
      showToast(err.message, "error");
      return false;
    }
  });
  // withBusy returns undefined when it refused a second click on a busy
  // control; that is not a successful confirmation.
  return outcome === true;
}

async function dismissVoiceCluster(clusterId, trigger = triggerButton()) {
  if (!window.confirm(
    "Mark this voice as noise or crosstalk? It will leave speaker review without being assigned to a person. No audio, transcript, or minutes will be deleted.",
  )) return;
  await withBusy(trigger, "Dismissing\u2026", async () => {
    try {
      const res = await fetch("/api/voices/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cluster_id: clusterId }),
      });
      if (!res.ok) throw new Error("Failed to dismiss cluster");
      showToast("Marked this fragment as non-speaker noise. No meeting content was deleted.", "info");
      await loadVoiceClusters();
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

function openVoiceNameModal(clusterId) {
  const cluster = (state.voiceClusters || []).find((c) => c.id === clusterId);
  if (!cluster) return;

  $("voice-name-cluster-id").value = clusterId;
  $("voice-new-person-name").value = "";
  const people = [...(state.people || [])].sort((a, b) => a.canonical.localeCompare(b.canonical));
  $("voice-existing-person").innerHTML = [
    '<option value="">Choose an existing speaker…</option>',
    ...people.map((person) => {
      const detail = person.role ? ` — ${person.role}` : "";
      return `<option value="${escapeHtml(person.canonical)}">${escapeHtml(person.canonical + detail)}</option>`;
    }),
  ].join("");

  const useExisting = people.length > 0;
  $("voice-name-existing-mode").checked = useExisting;
  $("voice-name-existing-mode").disabled = !useExisting;
  $("voice-name-new-mode").checked = !useExisting;
  $("voice-name-modal-sub").textContent =
    `This voice appears in ${cluster.size} meeting${cluster.size === 1 ? "" : "s"}. ` +
    "Your choice will update every matching meeting.";
  updateVoiceNameMode();
  $("voice-name-modal").showModal();
  setTimeout(() => (useExisting ? $("voice-existing-person") : $("voice-new-person-name")).focus(), 50);
}

function updateVoiceNameMode() {
  const useExisting = $("voice-name-existing-mode").checked;
  $("voice-existing-panel").hidden = !useExisting;
  $("voice-new-panel").hidden = useExisting;
  $("voice-existing-person").disabled = !useExisting;
  $("voice-existing-person").required = useExisting;
  $("voice-new-person-name").disabled = useExisting;
  $("voice-new-person-name").required = !useExisting;
}

function closeVoiceNameModal() {
  $("voice-name-modal").close();
}

async function submitVoiceNameModal() {
  const useExisting = $("voice-name-existing-mode").checked;
  const name = (useExisting ? $("voice-existing-person").value : $("voice-new-person-name").value).trim();
  if (!name) return;

  const save = $("voice-name-save");
  save.disabled = true;
  save.textContent = "Saving…";
  const saved = await confirmVoiceCluster($("voice-name-cluster-id").value, name, null);
  save.disabled = false;
  save.textContent = "Confirm Speaker";
  if (saved) {
    stopClusterPlayback();
    closeVoiceNameModal();
  }
}

// ── Decision Timeline Evolution ──────────────────────────────────────
function switchAskMode(mode) {
  const qaBtn = $("mode-btn-qa");
  const tlBtn = $("mode-btn-timeline");
  const qaPanel = $("ask-qa-panel");
  const tlPanel = $("ask-timeline-panel");

  if (mode === "timeline") {
    if (qaBtn) qaBtn.classList.remove("active");
    if (tlBtn) tlBtn.classList.add("active");
    if (qaBtn) qaBtn.setAttribute("aria-selected", "false");
    if (qaBtn) qaBtn.setAttribute("tabindex", "-1");
    if (tlBtn) tlBtn.setAttribute("aria-selected", "true");
    if (tlBtn) tlBtn.setAttribute("tabindex", "0");
    if (qaPanel) qaPanel.style.display = "none";
    if (tlPanel) tlPanel.style.display = "block";
    loadTimelineForTopic("all");
  } else {
    if (tlBtn) tlBtn.classList.remove("active");
    if (qaBtn) qaBtn.classList.add("active");
    if (tlBtn) tlBtn.setAttribute("aria-selected", "false");
    if (tlBtn) tlBtn.setAttribute("tabindex", "-1");
    if (qaBtn) qaBtn.setAttribute("aria-selected", "true");
    if (qaBtn) qaBtn.setAttribute("tabindex", "0");
    if (tlPanel) tlPanel.style.display = "none";
    if (qaPanel) qaPanel.style.display = "block";
  }
}

async function loadTimelineForTopic(topic, trigger = triggerButton()) {
  const input = $("timeline-topic-input");
  if (input && topic !== "all") input.value = topic;
  const container = $("timeline-results");
  if (!container) return;

  container.innerHTML = `<div class="empty-timeline"><p>Compiling chronological decision evolution for <strong>${escapeHtml(topic === "all" ? "All Projects" : topic)}</strong>...</p></div>`;

  await withBusy(trigger, null, async () => {
    try {
      const res = await fetch(`/api/timeline?topic=${encodeURIComponent(topic)}`);
      if (!res.ok) throw new Error("Failed to load timeline");
      const data = await res.json();
      renderTimeline(data);
    } catch (err) {
      container.innerHTML = `<div class="empty-timeline"><p style="color:var(--clay)">Failed to load timeline: ${escapeHtml(err.message)}</p></div>`;
    }
  });
}

function renderTimeline(data) {
  const container = $("timeline-results");
  if (!container) return;

  if (!data.events || !data.events.length) {
    container.innerHTML = `
      <div class="empty-timeline">
        <p>No historical decisions found matching "<strong>${escapeHtml(data.topic)}</strong>".</p>
        <button type="button" class="btn-outline btn-sm" onclick="loadTimelineForTopic('all')">View All Historical Milestones</button>
      </div>
    `;
    return;
  }

  const eventsHtml = data.events
    .map((evt, idx) => {
      const attendeesHtml = (evt.speakers || []).length
        ? `<div class="timeline-attendees">👥 ${evt.speakers.map((s) => `<span class="chip">${escapeHtml(s)}</span>`).join(" ")}</div>`
        : "";

      const decisionsList = (evt.decisions || [])
        .map((d) => `<li>${escapeHtml(d)}</li>`)
        .join("");

      return `
        <div class="timeline-node">
          <div class="timeline-marker">
            <span class="timeline-dot"></span>
            <span class="timeline-num">#${idx + 1}</span>
          </div>
          <div class="timeline-card">
            <div class="timeline-card-header">
              <span class="timeline-date">📅 ${escapeHtml(evt.date)} · ${escapeHtml(evt.time || "")}</span>
              <button type="button" class="btn-link" onclick="jumpToMeeting('${evt.meeting_id}')">↗ View Full Brief</button>
            </div>
            <h4 class="timeline-title">${escapeHtml(evt.title)}</h4>
            <div class="timeline-headline">📌 <strong>${escapeHtml(evt.headline)}</strong></div>
            ${decisionsList ? `<ul class="timeline-decisions">${decisionsList}</ul>` : ""}
            ${attendeesHtml}
          </div>
        </div>
      `;
    })
    .join("");

  container.innerHTML = `
    <div class="timeline-summary-bar">
      <span>Showing <strong>${data.total_milestones}</strong> chronological milestone${data.total_milestones > 1 ? "s" : ""} for "<strong>${escapeHtml(data.topic)}</strong>"</span>
    </div>
    <div class="timeline-track">${eventsHtml}</div>
  `;
}

function jumpToMeeting(meetingId) {
  // Switch to library tab
  const libBtn = document.querySelector('[data-tab="tab-ledger"]');
  if (libBtn) libBtn.click();
  selectMeeting(meetingId);
}

// ── Application Initialization ───────────────────────────────────────
function init() {
  // Before any load(): the first render must already honour the saved choices,
  // or the list visibly re-sorts itself a beat after it appears.
  loadControlState();
  setupTabs();
  setupAskModeTabs();
  setupEventListeners();
  setupAskPanel();
  setupCommitmentsPanel();
  loadOverview();
  loadMeetings();
  loadPeople();
  loadCommitments();
  loadVoiceClusters();
  checkPipelineStatus();
  setInterval(() => {
    loadOverview();
    loadCommitments();
    checkPipelineStatus();
  }, 8000);
}

document.addEventListener("DOMContentLoaded", init);


function formatShortDate(dateStr) {
  if (!dateStr) return "—";
  const parts = dateStr.split("-");
  if (parts.length === 3) {
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const m = parseInt(parts[1], 10) - 1;
    return `${months[m] || parts[1]} ${parts[2]}`;
  }
  return dateStr;
}

// ── Transcript reader ────────────────────────────────────────────────
// The minutes cite [H:MM:SS] on every decision, and the alignment stage exists
// to make those citations checkable - but the transcripts were retained on disk
// and never served. Loaded on demand rather than with the meeting: these run to
// hundreds of segments and most visits never open one.
async function toggleTranscript(meetingId) {
  const body = $("transcript-body");
  const button = $("btn-transcript");
  if (!body || !button) return;

  if (!body.hidden) {
    body.hidden = true;
    button.textContent = "Show full transcript";
    return;
  }

  body.hidden = false;
  button.textContent = "Hide transcript";

  if (body.dataset.loadedFor === meetingId) return;

  body.innerHTML = `<p style="color:var(--muted);">Loading transcript…</p>`;
  try {
    const res = await fetch(`/api/meetings/${encodeURIComponent(meetingId)}/transcript`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || "No transcript stored for this meeting.");
    }
    const data = await res.json();
    const unresolved = (data.unresolved || []).length;
    const notice = unresolved
      ? `<p style="color:var(--muted); font-size:0.8rem;">${unresolved} speaker${
          unresolved > 1 ? "s" : ""
        } still unnamed — name them above and reopen to see the transcript update.</p>`
      : "";
    body.innerHTML = `
      <p style="color:var(--muted); font-size:0.8rem;">
        ${data.segment_count} segments · ${escapeHtml(data.model)} · ${escapeHtml(data.language)}
      </p>
      ${notice}
      <div class="minutes">${renderMarkdown(data.markdown)}</div>
    `;
    body.dataset.loadedFor = meetingId;
  } catch (err) {
    body.innerHTML = `<p style="color:var(--clay)">${escapeHtml(err.message)}</p>`;
  }
}

async function exportToProductManager() {
  const btn = $("btn-export-pm");
  const origText = btn ? btn.textContent : "📁 Push to Product Manager";
  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "⏳ Syncing to PM...";
    }
    const res = await fetch("/api/export/product-manager", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      showToast(
        `✓ Synced ${data.synced} professional minutes to Product Manager (${data.quarantined_personal} personal kept private)`,
        "success",
      );
    } else {
      showToast(`Export failed: ${data.error || "Unknown error"}`, "error");
    }
  } catch (err) {
    showToast(`Failed to reach export endpoint: ${err.message}`, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }
}
