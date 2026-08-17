/**
 * Meeting Memory — Interactive Control Room & Archive
 */

let state = {
  activeMeetingId: null,
  meetings: [],
  people: [],
  pipelineRunning: false,
  pollTimer: null,
};

// ── DOM Helpers ───────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Initialization ───────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupEventListeners();
  loadOverview();
  loadMeetings();
  loadPeople();
  checkPipelineStatus();
});

// ── Tabs Navigation ──────────────────────────────────────────────────
function setupTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      $$(".tab-pane").forEach((p) => p.classList.remove("active"));

      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
      const target = btn.dataset.tab;
      if ($(target)) {
        $(target).classList.add("active");
      }
    });
  });
}

function setupEventListeners() {
  // Search & Filter
  $("meeting-search").addEventListener("input", () => filterAndRenderMeetings());
  $("meeting-filter-status").addEventListener("change", () => filterAndRenderMeetings());

  // Quick Run & Refresh
  $("btn-quick-run").addEventListener("click", () => runStage("all"));
  $("btn-refresh").addEventListener("click", () => {
    loadOverview();
    loadMeetings();
    loadPeople();
    checkPipelineStatus();
    showToast("Refreshed dashboard data");
  });

  // Query / RAG Form
  $("query-form").addEventListener("submit", (e) => {
    e.preventDefault();
    askArchive();
  });

  // People Forms
  $("form-add-person").addEventListener("submit", (e) => {
    e.preventDefault();
    submitAddPerson();
  });

  $("form-merge-person").addEventListener("submit", (e) => {
    e.preventDefault();
    submitMergePerson();
  });

  // Speaker Modal Form
  $("speaker-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitSpeakerModal();
  });
}

// ── Overview & Metrics ───────────────────────────────────────────────
async function loadOverview() {
  try {
    const res = await fetch("/api/overview");
    if (!res.ok) throw new Error("Overview unavailable");
    const data = await res.json();
    renderOverview(data);
  } catch (err) {
    $("rag-health").textContent = "Index unavailable";
    console.error("Failed to load overview:", err);
  }
}

function renderOverview(data) {
  // Masthead & Signal Strip
  $("rag-health").textContent = `Index: ${data.lightrag || "healthy"}`;
  $("metric-meetings").textContent = data.meetings ?? "0";
  $("metric-indexed").textContent = data.indexed ?? "0";

  const yest = data.activity?.yesterday || {};
  $("metric-yesterday").textContent = `${yest.count || 0} (${yest.duration_min || 0}m)`;

  const today = data.activity?.today || {};
  $("metric-today").textContent = `${today.count || 0} (${today.duration_min || 0}m)`;

  $("metric-hours").textContent = `${data.durations?.total_hours || 0}h`;
  const attentionCount = (data.failed || 0) + (data.speaker_review || 0);
  $("metric-attention").textContent = attentionCount;

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

  // Stage Timings Table
  renderTimingsTable(data.timings || []);

  // Top Graph Entities Table
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
  if (!entities.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-cell">No knowledge graph entities found.</td></tr>`;
    return;
  }
  tbody.innerHTML = entities
    .map(
      (e) => `
    <tr>
      <td><strong>${escapeHtml(e.name)}</strong></td>
      <td><span class="chip">${escapeHtml(e.kind || "entity")}</span></td>
      <td>${e.mention_count || 1}</td>
    </tr>
  `
    )
    .join("");
}

// ── Pipeline Orchestrator & Live Logs ────────────────────────────────
async function checkPipelineStatus() {
  try {
    const res = await fetch("/api/pipeline/status");
    if (!res.ok) return;
    const status = await res.json();
    handlePipelineStatus(status);
  } catch (err) {
    console.error("Status check failed:", err);
  }
}

function handlePipelineStatus(status) {
  const indicator = $("pipeline-indicator");
  const statusText = $("pipeline-status-text");
  const terminalBody = $("terminal-body");

  state.pipelineRunning = status.running;

  if (status.running) {
    indicator.classList.add("running");
    statusText.textContent = `Pipeline: Running [${status.stage}]`;
    $("btn-quick-run").disabled = true;
    $("btn-quick-run").textContent = `⏳ Running [${status.stage}]...`;
  } else {
    indicator.classList.remove("running");
    statusText.textContent = "Pipeline Idle";
    $("btn-quick-run").disabled = false;
    $("btn-quick-run").textContent = "▶ Run Pipeline";
  }

  // Update logs
  if (status.logs && status.logs.length) {
    terminalBody.textContent = status.logs.join("\n");
    terminalBody.scrollTop = terminalBody.scrollHeight;
  }

  // Schedule next poll if running
  if (status.running) {
    if (!state.pollTimer) {
      state.pollTimer = setTimeout(async () => {
        state.pollTimer = null;
        await checkPipelineStatus();
        loadOverview();
      }, 1500);
    }
  } else if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

async function runStage(stage) {
  if (state.pipelineRunning) {
    showToast("A pipeline stage is already running.", "error");
    return;
  }
  try {
    const res = await fetch("/api/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to start stage");
    showToast(`Started pipeline stage '${stage}'`, "success");
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
    showToast(`Requeued ${data.requeued} failed meetings`, "success");
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
    if (!res.ok) throw new Error(data.error || "Recompile failed");
    showToast("Recompilation stage initiated", "success");
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
    showToast("Meeting requeued for ASR transcription", "success");
    loadMeetings();
    selectMeeting(meetingId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

function clearTerminalLogs() {
  $("terminal-body").textContent = "Terminal log display cleared.";
}

// ── Meetings Archive & Reader ────────────────────────────────────────
async function loadMeetings() {
  try {
    const res = await fetch("/api/meetings");
    if (!res.ok) throw new Error("Meetings library unavailable");
    const data = await res.json();
    state.meetings = data.meetings || [];
    filterAndRenderMeetings();
  } catch (err) {
    $("meeting-list").innerHTML = `<div class="empty-list">Failed to load meetings.</div>`;
  }
}

function filterAndRenderMeetings() {
  const query = ($("meeting-search").value || "").toLowerCase().trim();
  const filter = $("meeting-filter-status").value;

  const filtered = state.meetings.filter((m) => {
    if (filter === "ready" && m.status !== "indexed") return false;
    if (filter === "review" && m.unresolved_count === 0) return false;
    if (filter === "failed" && m.status !== "failed") return false;

    if (!query) return true;
    return (
      (m.title || "").toLowerCase().includes(query) ||
      (m.date || "").toLowerCase().includes(query) ||
      (m.source_name || "").toLowerCase().includes(query) ||
      (m.excerpt || "").toLowerCase().includes(query)
    );
  });

  renderMeetingList(filtered);
}

function renderMeetingList(meetings) {
  const container = $("meeting-list");
  if (!meetings.length) {
    container.innerHTML = `<div class="empty-list">No meetings match your filter.</div>`;
    return;
  }

  container.innerHTML = meetings
    .map((m) => {
      const activeClass = m.id === state.activeMeetingId ? "active" : "";
      const isFailed = m.status === "failed";
      const badgeClass = isFailed ? "badge warn" : "badge";
      const badgeText = isFailed ? "Failed" : m.review_state || m.status;

      return `
      <button type="button" class="meeting-card ${activeClass}" onclick="selectMeeting('${m.id}')">
        <div class="meeting-date">${formatShortDate(m.date)}</div>
        <div>
          <h3>${escapeHtml(m.title)}</h3>
          <p>${escapeHtml(m.excerpt || "No minutes preview available.")}</p>
          <span class="${badgeClass}">${escapeHtml(badgeText)}</span>
          <span class="badge" style="background:var(--paper);border:1px solid var(--rule);">${formatDuration(m.duration_sec)}</span>
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
  // Update active state in list
  $$(".meeting-card").forEach((card) => card.classList.remove("active"));
  const foundCard = Array.from($$(".meeting-card")).find((c) =>
    c.getAttribute("onclick")?.includes(id)
  );
  if (foundCard) foundCard.classList.add("active");

  const reader = $("meeting-reader");
  reader.innerHTML = `<p class="reader-kicker">LOADING RECORD...</p>`;

  try {
    const res = await fetch(`/api/meetings/${id}`);
    if (!res.ok) throw new Error("Meeting detail unavailable");
    const m = await res.json();
    renderMeetingDetail(m);
  } catch (err) {
    reader.innerHTML = `<div class="empty-list">Failed to load meeting minutes.</div>`;
  }
}

function renderMeetingDetail(m) {
  const reader = $("meeting-reader");
  const isFailed = m.status === "failed";

  let failedBanner = "";
  if (isFailed) {
    failedBanner = `
      <div class="failed-banner">
        <div>
          <strong>Stage Error:</strong>
          <p>${escapeHtml(m.error || "Unknown pipeline execution error.")}</p>
        </div>
        <button type="button" class="btn-action btn-warn" onclick="retrySingleMeeting('${m.id}')">↻ Retry Meeting</button>
      </div>
    `;
  }

  const driveLink = m.drive_url
    ? `<div class="reader-actions"><a href="${escapeHtml(m.drive_url)}" target="_blank" rel="noopener">Open original audio in Google Drive ↗</a></div>`
    : "";

  const speakersHtml = (m.speakers || [])
    .map((s) => {
      const isUnresolved = !s.name;
      const chipClass = isUnresolved ? "chip chip-speaker unresolved" : "chip chip-speaker";
      const displayName = s.name ? `${s.label} · ${s.name}` : `${s.label} · (Unassigned ✎)`;
      // Values go in data-* attributes, never into an inline handler. The
      // browser HTML-decodes an attribute before parsing it as JS, so an
      // escaped quote turns back into a real one and escapes the string.
      // Speaker names are user- and LLM-supplied, so that is reachable.
      return `<button type="button" class="${chipClass}" title="Click to resolve speaker identity" data-speaker-chip data-meeting-id="${escapeHtml(m.id)}" data-label="${escapeHtml(s.label)}" data-name="${escapeHtml(s.name || "")}">${escapeHtml(displayName)}</button>`;
    })
    .join("");

  const entitiesHtml = (m.entities || [])
    .map((e) => `<span class="chip" title="${escapeHtml(e.description || "")}">${escapeHtml(e.name)}</span>`)
    .join("");

  reader.innerHTML = `
    <p class="reader-kicker">${escapeHtml(m.date || "UNDATED")} · ${escapeHtml(m.time || "")} · ${formatDuration(m.duration_sec)}</p>
    <h2>${escapeHtml(m.title)}</h2>
    <div class="reader-meta">
      <span>ID: <code>${m.short_id}</code></span>
      <span>Source: ${escapeHtml(m.source_name || "direct")}</span>
      <span>Status: <strong>${escapeHtml(m.status)}</strong></span>
    </div>

    ${failedBanner}
    ${driveLink}

    <div class="speaker-section">
      <p class="eyebrow">ATTRIBUTED SPEAKERS</p>
      <div class="speaker-list">
        ${speakersHtml || '<span class="chip">No speaker diarization</span>'}
      </div>
    </div>

    ${
      entitiesHtml
        ? `
      <div class="entity-section" style="margin-top:14px;">
        <p class="eyebrow">GRAPH ENTITIES</p>
        <div class="entity-list">${entitiesHtml}</div>
      </div>
    `
        : ""
    }

    <div class="minutes">${escapeHtml(m.minutes || "No compiled minutes recorded for this meeting yet.")}</div>
  `;

  // Bind after innerHTML: dataset values are plain strings, never parsed as JS.
  reader.querySelectorAll("[data-speaker-chip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      openSpeakerModal(btn.dataset.meetingId, btn.dataset.label, btn.dataset.name);
    });
  });
}

// ── People & Aliases Management ──────────────────────────────────────
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
  if (!people.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-cell">No registered people yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = people
    .map(
      (p) => `
    <tr>
      <td><strong>${escapeHtml(p.canonical)}</strong></td>
      <td>${escapeHtml(p.role || "—")}</td>
      <td>${p.meeting_count || 0}</td>
      <td>${(p.aliases || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join(" ") || "—"}</td>
    </tr>
  `
    )
    .join("");
}

function updateDatalistSuggestions(people) {
  const datalist = $("canonical-suggestions");
  datalist.innerHTML = people
    .map((p) => `<option value="${escapeHtml(p.canonical)}">${escapeHtml(p.role ? `${p.canonical} (${p.role})` : p.canonical)}</option>`)
    .join("");
}

async function submitAddPerson() {
  const canonical = $("person-canonical").value.trim();
  const role = $("person-role").value.trim() || null;
  const aliasesRaw = $("person-aliases").value.trim();
  const aliases = aliasesRaw ? aliasesRaw.split(",").map((s) => s.trim()).filter(Boolean) : null;

  if (!canonical) return;
  try {
    const res = await fetch("/api/people", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical, role, aliases }),
    });
    if (!res.ok) throw new Error("Failed to save person");
    showToast(`Saved person: ${canonical}`, "success");
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
    if (!res.ok) throw new Error(data.error || "Failed to merge people");
    showToast(`Merged '${fromName}' into '${into}' (${data.rewritten} records rewritten)`, "success");
    $("merge-from").value = "";
    $("merge-into").value = "";
    loadPeople();
    loadOverview();
    if (state.activeMeetingId) selectMeeting(state.activeMeetingId);
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ── Speaker Modal Dialog ─────────────────────────────────────────────
function openSpeakerModal(meetingId, label, currentName) {
  $("modal-meeting-id").value = meetingId;
  $("modal-speaker-label").value = label;
  $("modal-speaker-name").value = currentName || "";
  $("speaker-modal-sub").textContent = `Assign canonical person for speaker tag: ${label}`;
  $("speaker-modal").showModal();
}

function closeSpeakerModal() {
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
    showToast(`Assigned ${label} → ${name}`, "success");
    closeSpeakerModal();
    selectMeeting(meetingId);
    loadPeople();
    loadOverview();
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ── Ask Archive (RAG Search) ─────────────────────────────────────────
async function askArchive() {
  const question = $("question").value.trim();
  const mode = $("query-mode").value;
  const answerEl = $("answer");

  if (!question) {
    showToast("Please enter a question.", "error");
    return;
  }

  answerEl.className = "answer";
  answerEl.innerHTML = `<p>Consulting local hybrid LightRAG graph and synthesising record...</p>`;

  try {
    const res = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, mode }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Query failed");

    answerEl.innerHTML = `
      <div>${escapeHtml(data.answer)}</div>
      <span class="answer-meta">
        Latency: ${data.retrieval_sec}s retrieval + ${data.synthesis_sec}s synthesis · Provider: ${escapeHtml(data.provider || "ollama")} · Mode: ${escapeHtml(mode)}
      </span>
    `;
  } catch (err) {
    answerEl.className = "answer empty";
    answerEl.innerHTML = `<p style="color:var(--clay)">Error querying archive: ${escapeHtml(err.message)}</p>`;
  }
}

// ── Utilities & Formatting ───────────────────────────────────────────
function showToast(message, type = "info") {
  const container = $("toast-container");
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

// ── Quick Jump (API Explorer) ────────────────────────────────────────
function jumpToTab(tabId) {
  // Activate the target tab
  $$(".tab-btn").forEach((b) => {
    b.classList.remove("active");
    b.setAttribute("aria-selected", "false");
  });
  $$(".tab-pane").forEach((p) => p.classList.remove("active"));

  const targetTab = $(tabId);
  if (targetTab) {
    targetTab.classList.add("active");
  }

  // Find and activate the matching tab button
  $$(".tab-btn").forEach((btn) => {
    if (btn.dataset.tab === tabId) {
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
    }
  });

  // Scroll to top of the page
  window.scrollTo({ top: 0, behavior: "smooth" });
}
