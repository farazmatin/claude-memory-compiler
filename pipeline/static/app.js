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
    showToast("Refreshed archive data", "success");
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
  const attentionCount = (data.failed || 0) + (data.speaker_review || 0);
  $("metric-attention").textContent = attentionCount > 0 ? `${attentionCount} items` : "0";

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

// ── Pipeline Orchestrator & Live Status ──────────────────────────────
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
    statusText.textContent = "All Caught Up";
    $("btn-quick-run").disabled = false;
    $("btn-quick-run").textContent = "▶ Sync & Process Recordings";
  }

  // Update logs in diagnostics drawer
  if (terminalBody && status.logs && status.logs.length) {
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
        loadMeetings();
      }, 1500);
    }
  } else if (state.pollTimer) {
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
    container.innerHTML = `<div class="empty-list">No meetings match your search.</div>`;
    return;
  }

  container.innerHTML = meetings
    .map((m) => {
      const activeClass = m.id === state.activeMeetingId ? "active" : "";
      const isFailed = m.status === "failed";
      const badgeClass = isFailed ? "badge warn" : "badge";
      
      let badgeText = "Ready";
      if (isFailed) badgeText = "Needs Attention";
      else if (m.unresolved_count > 0) badgeText = `${m.unresolved_count} Unnamed Speaker${m.unresolved_count > 1 ? "s" : ""}`;
      else if (m.status === "indexed") badgeText = "Search Ready";
      else badgeText = m.review_state || m.status;

      return `
      <button type="button" class="meeting-card ${activeClass}" onclick="selectMeeting('${m.id}')">
        <div class="meeting-date">${formatShortDate(m.date)}</div>
        <div>
          <h3>${escapeHtml(m.title)}</h3>
          <p>${escapeHtml(m.excerpt || "No summary available.")}</p>
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

  const driveLink = m.drive_url
    ? `<div class="reader-actions"><a href="${escapeHtml(m.drive_url)}" target="_blank" rel="noopener">🎧 Listen to Original Audio in Google Drive ↗</a></div>`
    : "";

  const speakersHtml = (m.speakers || [])
    .map((s) => {
      const isUnresolved = !s.name;
      const chipClass = isUnresolved ? "chip chip-speaker unresolved" : "chip chip-speaker";
      const displayName = s.name ? `👤 ${s.name} (${s.label})` : `🎙️ ${s.label} · (Click to Name ✎)`;
      return `<button type="button" class="${chipClass}" title="Click to name this person" data-speaker-chip data-meeting-id="${escapeHtml(m.id)}" data-label="${escapeHtml(s.label)}" data-name="${escapeHtml(s.name || "")}">${escapeHtml(displayName)}</button>`;
    })
    .join("");

  const entitiesHtml = (m.entities || [])
    .map((e) => `<span class="chip" title="${escapeHtml(e.description || "")}">${escapeHtml(e.name)}</span>`)
    .join("");

  const formattedMinutes = renderMarkdown(m.minutes || "No executive brief recorded for this meeting yet.");

  reader.innerHTML = `
    <p class="reader-kicker">${escapeHtml(m.date || "UNDATED")} · ${escapeHtml(m.time || "")} · ${formatDuration(m.duration_sec)}</p>
    <h2>${escapeHtml(m.title)}</h2>
    <div class="reader-meta">
      <span>Meeting ID: <code>${m.short_id}</code></span>
      <span>Source: ${escapeHtml(m.source_name || "direct recording")}</span>
      <span>Status: <strong>${escapeHtml(m.status === "indexed" ? "Search Ready" : m.status)}</strong></span>
    </div>

    ${failedBanner}
    ${driveLink}

    <div class="speaker-section">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <p class="eyebrow" style="margin:0;">ATTENDEES &amp; SPEAKERS (CLICK TO NAME)</p>
        <button type="button" class="btn-outline" style="font-size:0.75rem; padding:4px 10px; border-radius:4px;" onclick="openSpeakerModal('${escapeHtml(m.id)}', '', '')">+ Add Attendee / Speaker</button>
      </div>
      <div class="speaker-list">
        ${speakersHtml || '<span class="chip" style="opacity:0.75;">No speakers diarized yet — click \"+ Add Attendee\" to add</span>'}
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
  `;

  // Bind click handlers to speaker chips
  reader.querySelectorAll("[data-speaker-chip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      openSpeakerModal(btn.dataset.meetingId, btn.dataset.label, btn.dataset.name);
    });
  });
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
      (p) => `
    <tr>
      <td><strong>${escapeHtml(p.canonical)}</strong></td>
      <td>${escapeHtml(p.role || "—")}</td>
      <td>${p.meetings || p.meeting_count || 0} meetings</td>
      <td>${(p.aliases || []).map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join(" ") || "—"}</td>
    </tr>
  `
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
  const aliases = aliasesRaw ? aliasesRaw.split(",").map((s) => s.trim()).filter(Boolean) : null;

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

// ── Speaker Modal Dialog ─────────────────────────────────────────────
function openSpeakerModal(meetingId, label, currentName) {
  $("modal-meeting-id").value = meetingId;
  
  if (!label) {
    // Generate next available label
    const activeDetail = state.meetings.find(m => m.id === meetingId);
    label = "SPEAKER_00";
  }

  $("modal-speaker-label").value = label;
  $("modal-speaker-name").value = currentName || "";
  $("speaker-modal-sub").textContent = `Assign contact name to speaker tag: ${label}`;
  $("speaker-modal").showModal();
  setTimeout(() => $("modal-speaker-name").focus(), 50);
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
    showToast(`Saved speaker: ${label} → ${name}`, "success");
    closeSpeakerModal();
    selectMeeting(meetingId);
    loadPeople();
    loadOverview();
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ── Ask AI Assistant ─────────────────────────────────────────────────
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
      body: JSON.stringify({ question, mode }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Query could not be completed");

    answerEl.innerHTML = `
      <div>${renderMarkdown(data.answer)}</div>
      <span class="answer-meta">
        Retrieved in ${data.retrieval_sec}s · Mode: ${escapeHtml(mode)}
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

  const cardsHtml = clusters
    .map((c) => {
      const bestMatchName = c.best_canonical;
      const matchConfidence = c.best_score ? Math.round(c.best_score * 100) : null;
      const matchText = bestMatchName
        ? `Best Match: <strong>${escapeHtml(bestMatchName)}</strong> <span class="similarity-pill">${matchConfidence}% confidence</span>`
        : `Unrecognized recurring speaker`;

      const confirmBtn = bestMatchName
        ? `<button type="button" class="btn-action btn-sm" onclick="confirmVoiceCluster('${c.id}', '${escapeHtml(bestMatchName)}')">✓ Confirm as ${escapeHtml(bestMatchName)}</button>`
        : "";

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
            ${confirmBtn}
            <button type="button" class="btn-outline btn-sm" onclick="promptCustomVoiceName('${c.id}')">✎ Name Someone Else</button>
            <button type="button" class="btn-text btn-sm" onclick="dismissVoiceCluster('${c.id}')">✗ Dismiss Noise</button>
          </div>
        </div>
      `;
    })
    .join("");

  if (banner) {
    banner.innerHTML = `
      <div class="voice-banner-inner">
        <div class="voice-banner-header">
          <h3>🎙️ Speaker Identity Review (${clusters.length} pending)</h3>
          <p>We detected recurring voices across your meetings. Confirming them enrolls their voiceprint and updates all past meetings automatically.</p>
        </div>
        <div class="voice-cards-scroll">${cardsHtml}</div>
      </div>
    `;
    banner.style.display = "block";
  }

  if (peopleSection && peopleList) {
    peopleList.innerHTML = cardsHtml;
    peopleSection.style.display = "block";
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
  if (name && name.trim()) {
    confirmVoiceCluster(clusterId, name.trim());
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
    if (qaPanel) qaPanel.style.display = "none";
    if (tlPanel) tlPanel.style.display = "block";
    loadTimelineForTopic("all");
  } else {
    if (tlBtn) tlBtn.classList.remove("active");
    if (qaBtn) qaBtn.classList.add("active");
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
  setupEventListeners();
  loadOverview();
  loadMeetings();
  loadPeople();
  loadVoiceClusters();

  // Poll status periodically
  setInterval(() => {
    loadPipelineStatus();
    loadOverview();
  }, 10000);
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
