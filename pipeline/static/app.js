/**
 * Meeting Memory — Executive Archive & Interactive Assistant
 */

let state = {
  activeMeetingId: null,
  meetings: [],
  people: [],
  pipelineRunning: false,
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
const VOICE_LIBRARY_LIMIT = 3;

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
  $("meeting-filter-status").addEventListener("change", () => filterAndRenderMeetings());
  if ($("meeting-filter-category")) {
    $("meeting-filter-category").addEventListener("change", () => filterAndRenderMeetings());
  }

  // Pipeline, export & refresh
  $("btn-quick-run").addEventListener("click", () => runStage("all"));
  $("btn-export-pm").addEventListener("click", exportToProductManager);
  $("btn-refresh").addEventListener("click", () => {
    loadOverview();
    loadMeetings();
    loadPeople();
    loadCommitments();
    checkPipelineStatus();
    showToast("Refreshed archive data", "success");
  });

  // Query / RAG Form
  $("query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    askArchive();
  });

  $("form-add-person").addEventListener("submit", (e) => {
    e.preventDefault();
    submitAddPerson();
  });
  $("form-merge-person").addEventListener("submit", (e) => {
    e.preventDefault();
    submitMergePerson();
  });
  $("speaker-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitSpeakerModal();
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
  state.pipelineRunning = status.running;

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
    $("btn-quick-run").disabled = false;
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
}

async function runStage(stage) {
  if (state.pipelineRunning) {
    showToast("Processing is already in progress.", "info");
    return;
  }
  try {
    const res = await fetch("/api/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to start operation");
    showToast(`Started audio processing (${stage})`, "success");
    checkPipelineStatus();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function retryAllFailed() {
  try {
    const res = await fetch("/api/pipeline/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "discovered" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Retry failed");
    showToast(`Requeued ${data.requeued} recordings for processing`, "success");
    loadOverview();
    loadMeetings();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function recompileStale() {
  try {
    const res = await fetch("/api/pipeline/recompile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Format refresh failed");
    showToast("Refreshing minutes layout", "success");
    checkPipelineStatus();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function retrySingleMeeting(meetingId) {
  try {
    const res = await fetch(`/api/meetings/${meetingId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "discovered" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Retry failed");
    showToast("Meeting requeued for transcription", "success");
    loadMeetings();
    selectMeeting(meetingId);
  } catch (err) {
    showToast(err.message, "error");
  }
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

function filterAndRenderMeetings() {
  const query = ($("meeting-search").value || "").toLowerCase().trim();
  const filter = $("meeting-filter-status").value;
  const categoryFilter = $("meeting-filter-category") ? $("meeting-filter-category").value : "all";

  const filtered = state.meetings.filter((m) => {
    if (categoryFilter !== "all" && m.category !== categoryFilter) return false;
    if (filter === "ready" && m.status !== "indexed") return false;
    if (filter === "review" && m.unresolved_count === 0) return false;
    if (filter === "failed" && m.status !== "failed") return false;

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

  renderMeetingList(filtered);
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
      <select id="meeting-category" onchange="updateMeetingCategory('${m.id}', this.value)" style="background:transparent;border:0;color:var(--ink);font-weight:600;">
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

async function updateMeetingCategory(meetingId, domain) {
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
    renderPeopleTable(state.people);
    updateDatalistSuggestions(state.people);
  } catch (err) {
    console.error("Failed to load people:", err);
  }
}

function renderPeopleTable(people) {
  const tbody = $("people-tbody");
  if (!tbody) return;
  if (!people.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No contacts added yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = people
    .map(
      (p) => {
        const aliases = Array.isArray(p.aliases)
          ? p.aliases
          : String(p.aliases || "").split(",").map((alias) => alias.trim()).filter(Boolean);
        const meetingCount = p.meetings || p.meeting_count || 0;
        return `
    <tr>
      <td><strong>${escapeHtml(p.canonical)}</strong></td>
      <td>${escapeHtml(p.role || "—")}</td>
      <td>${meetingCount} meeting${meetingCount === 1 ? "" : "s"}</td>
      <td>${aliases.map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join(" ") || "—"}</td>
    </tr>
  `;
      }
    )
    .join("");
}

function updateDatalistSuggestions(people) {
  const datalist = $("canonical-suggestions");
  if (!datalist) return;
  datalist.innerHTML = people
    .map((p) => `<option value="${escapeHtml(p.canonical)}">${escapeHtml(p.role ? `${p.canonical} (${p.role})` : p.canonical)}</option>`)
    .join("");
}

async function submitAddPerson() {
  const canonical = $("person-canonical").value.trim();
  const role = $("person-role").value.trim() || null;
  const aliasesRaw = $("person-aliases").value.trim();
  const aliases = aliasesRaw ? aliasesRaw.split(",").map((value) => value.trim()).filter(Boolean) : null;
  if (!canonical) return;
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
    loadPeople();
    loadOverview();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function submitMergePerson() {
  const fromName = $("merge-from").value.trim();
  const into = $("merge-into").value.trim();
  if (!fromName || !into) return;
  try {
    const res = await fetch("/api/people/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_name: fromName, into }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to combine names");
    showToast(`Combined '${fromName}' into '${into}' (${data.rewritten} records updated)`, "success");
    $("merge-from").value = "";
    $("merge-into").value = "";
    loadPeople();
    loadOverview();
    if (state.activeMeetingId) selectMeeting(state.activeMeetingId);
  } catch (err) {
    showToast(err.message, "error");
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

async function submitSpeakerModal() {
  const meetingId = $("modal-meeting-id").value;
  const label = $("modal-speaker-label").value;
  const name = $("modal-speaker-name").value.trim();
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
    loadPeople();
    loadOverview();
  } catch (err) {
    showToast(err.message, "error");
  }
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

  answerEl.className = "answer";
  answerEl.innerHTML = `<p>Consulting your meeting archive and preparing answer...</p>`;

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
  }
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

// ── Zero-Touch Voice Identification Cards (PR #4) ────────────────────
async function loadVoiceClusters() {
  try {
    const res = await fetch("/api/voices/clusters");
    if (!res.ok) return;
    const data = await res.json();
    state.voiceClusters = data.clusters || [];
    renderVoiceCards(state.voiceClusters);
  } catch (err) {
    console.error("Failed to load voice clusters:", err);
  }
}

function renderVoiceCards(clusters) {
  const banner = $("voice-review-banner");
  const peopleSection = $("voice-review-people-section");
  const peopleList = $("voice-review-people-list");

  if (!clusters || !clusters.length) {
    if (banner) banner.style.display = "none";
    if (peopleSection) peopleSection.style.display = "none";
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
        ? `<button type="button" class="btn-outline btn-sm js-voice-listen" data-cluster="${escapeHtml(c.id)}">▶ Listen (${clipSeconds}s)</button>`
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
          <div class="voice-card-actions">
            ${listenBtn}
            ${confirmBtn}
            ${transcriptSuggestion}
            <button type="button" class="btn-outline btn-sm js-voice-custom" data-cluster="${escapeHtml(c.id)}">✎ Name Someone Else</button>
            <button type="button" class="btn-text btn-sm js-voice-dismiss" data-cluster="${escapeHtml(c.id)}">✗ Dismiss Noise</button>
          </div>
        </div>
      `;
    });
  const cardsHtml = cards.join("");
  const libraryCardsHtml = cards.slice(0, VOICE_LIBRARY_LIMIT).join("");
  const reviewAllButton = clusters.length > VOICE_LIBRARY_LIMIT
    ? `<button type="button" class="btn-outline btn-sm js-review-all-voices" aria-controls="voice-review-people-section">Review all ${clusters.length} voices in People &amp; Team →</button>`
    : "";

  if (banner) {
    banner.innerHTML = `
      <div class="voice-banner-inner">
        <div class="voice-banner-header">
          <h3>🎙️ Speaker Identity Review (${clusters.length} recurring voices)</h3>
          <p>Showing the ${Math.min(clusters.length, VOICE_LIBRARY_LIMIT)} highest-impact voices. Confirming one updates every matching meeting.</p>
          <button type="button" class="btn-outline btn-sm" onclick="confirmConfidentVoiceClusters()">✓ Confirm High-Confidence Matches</button>
          ${reviewAllButton}
        </div>
        <div class="voice-cards-scroll">${libraryCardsHtml}</div>
      </div>
    `;
    banner.style.display = "block";
  }

  if (peopleSection && peopleList) {
    peopleList.innerHTML = cardsHtml;
    peopleSection.style.display = "block";
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(".js-meeting-transcript");
  if (button) {
    toggleTranscript(button.dataset.meeting);
    return;
  }
  const speaker = event.target.closest(".js-speaker-chip");
  if (speaker) {
    openSpeakerModal(speaker.dataset.meeting, speaker.dataset.label, speaker.dataset.name);
  }
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
  const reviewAll = event.target.closest(".js-review-all-voices");
  if (reviewAll) {
    const peopleTab = $("tab-button-knowledge");
    activateTab(peopleTab);
    requestAnimationFrame(() => $("voice-review-heading")?.focus());
    return;
  }
  const listen = event.target.closest(".js-voice-listen");
  if (listen) {
    playClusterClips(listen.dataset.cluster);
    return;
  }
  const confirm = event.target.closest(".js-voice-confirm");
  if (confirm) {
    confirmVoiceCluster(confirm.dataset.cluster, confirm.dataset.name);
    return;
  }
  const dismiss = event.target.closest(".js-voice-dismiss");
  if (dismiss) {
    dismissVoiceCluster(dismiss.dataset.cluster);
    return;
  }
  const custom = event.target.closest(".js-voice-custom");
  if (custom) promptCustomVoiceName(custom.dataset.cluster);
});

// Play every retained clip for a cluster back to back.
//
// One 6-second clip is not enough to recognise a voice, and the clips are
// separate files by construction: they are cut from moments at least 60s apart
// so three samples are three different sentences, not one sentence in thirds.
// Playing them in sequence is the closest thing to a continuous sample that the
// retained audio allows - the source recording is long gone.
let clusterPlayer = null;

function playClusterClips(clusterId) {
  const cluster = (state.voiceClusters || []).find((c) => c.id === clusterId);
  if (!cluster) return;

  const queue = [];
  for (const member of cluster.members || []) {
    for (let i = 0; i < (member.snippet_count || 0); i += 1) {
      queue.push(
        `/api/voices/snippet?meeting_id=${encodeURIComponent(member.meeting_id)}` +
          `&label=${encodeURIComponent(member.label)}&index=${i}`,
      );
    }
  }
  if (!queue.length) {
    showToast("No clip was retained for this voice.", "error");
    return;
  }

  // Stop whatever was playing first. Two cards playing at once is worse than
  // useless for telling two voices apart.
  if (clusterPlayer) {
    clusterPlayer.pause();
    clusterPlayer.onended = null;
    clusterPlayer.onerror = null;
  }
  clusterPlayer = new Audio();
  let at = 0;
  const advance = () => {
    if (at >= queue.length) {
      clusterPlayer = null;
      return;
    }
    clusterPlayer.src = queue[at];
    at += 1;
    clusterPlayer.play().catch(() => {});
  };
  clusterPlayer.onended = advance;
  // A missing file must not stall the queue - skip to the next clip.
  clusterPlayer.onerror = advance;
  advance();
}

async function confirmConfidentVoiceClusters() {
  const candidates = (state.voiceClusters || []).filter(
    (cluster) => cluster.best_canonical && (cluster.best_score || 0) >= 0.85,
  );
  if (!candidates.length) {
    showToast("No cluster is confident enough to accept automatically.", "error");
    return;
  }
  const names = [...new Set(candidates.map((cluster) => cluster.best_canonical))].join(", ");
  if (!window.confirm(`Confirm ${candidates.length} voice(s) as: ${names}?`)) return;
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
    loadVoiceClusters();
    loadMeetings();
    loadPeople();
    loadOverview();
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function confirmVoiceCluster(clusterId, canonical) {
  try {
    const res = await fetch("/api/voices/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cluster_id: clusterId, canonical }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to confirm voice");
    showToast(`Enrolled voiceprint for '${canonical}' across ${data.confirmed || "all"} meetings!`, "success");
    loadVoiceClusters();
    loadMeetings();
    loadPeople();
    loadOverview();
    if (state.activeMeetingId) selectMeeting(state.activeMeetingId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function dismissVoiceCluster(clusterId) {
  try {
    const res = await fetch("/api/voices/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cluster_id: clusterId }),
    });
    if (!res.ok) throw new Error("Failed to dismiss cluster");
    showToast("Dismissed speaker fragment as noise.", "info");
    loadVoiceClusters();
  } catch (err) {
    showToast(err.message, "error");
  }
}

function promptCustomVoiceName(clusterId) {
  const name = prompt("Enter the real contact name for this voice:");
  if (name && name.trim()) confirmVoiceCluster(clusterId, name.trim());
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

async function loadTimelineForTopic(topic) {
  const input = $("timeline-topic-input");
  if (input && topic !== "all") input.value = topic;
  const container = $("timeline-results");
  if (!container) return;

  container.innerHTML = `<div class="empty-timeline"><p>Compiling chronological decision evolution for <strong>${escapeHtml(topic === "all" ? "All Projects" : topic)}</strong>...</p></div>`;

  try {
    const res = await fetch(`/api/timeline?topic=${encodeURIComponent(topic)}`);
    if (!res.ok) throw new Error("Failed to load timeline");
    const data = await res.json();
    renderTimeline(data);
  } catch (err) {
    container.innerHTML = `<div class="empty-timeline"><p style="color:var(--clay)">Failed to load timeline: ${escapeHtml(err.message)}</p></div>`;
  }
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
