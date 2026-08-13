const request = async (url, options = {}) => {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "The request could not be completed.");
  return payload;
};

const state = { meetings: [], selectedId: null };
const displayDate = date => date || "Undated";
const duration = seconds => seconds ? `${Math.round(seconds / 60)} min` : "Duration unknown";
const setText = (id, value) => { document.getElementById(id).textContent = value; };

async function loadOverview() {
  const data = await request("/api/overview");
  setText("meeting-count", data.meetings);
  setText("indexed-count", data.indexed);
  setText("review-count", data.speaker_review);
  setText("failed-count", data.failed);
  setText("rag-health", `Local index: ${data.lightrag}`);
}

function renderList() {
  const list = document.getElementById("meeting-list");
  list.replaceChildren();
  if (!state.meetings.length) { list.innerHTML = '<p class="empty-list">No meetings match this filter yet.</p>'; return; }
  for (const meeting of state.meetings) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `meeting-card ${meeting.id === state.selectedId ? "active" : ""}`;
    card.innerHTML = `<time class="meeting-date">${displayDate(meeting.date).slice(5).replace("-", "/")}</time><span><h3>${escapeHtml(meeting.title)}</h3><p>${escapeHtml(meeting.excerpt || meeting.source_name || "No minutes yet.")}</p><b class="badge ${meeting.review_state === "Ready" ? "" : "warn"}">${escapeHtml(meeting.review_state)}</b></span>`;
    card.addEventListener("click", () => selectMeeting(meeting.id));
    list.append(card);
  }
}

async function loadMeetings(search = "") {
  const data = await request(`/api/meetings?search=${encodeURIComponent(search)}`);
  state.meetings = data.meetings;
  renderList();
  if (!state.selectedId && state.meetings[0]) selectMeeting(state.meetings[0].id);
}

async function selectMeeting(id) {
  state.selectedId = id;
  renderList();
  const meeting = await request(`/api/meetings/${encodeURIComponent(id)}`);
  const reader = document.getElementById("meeting-reader");
  const speakerChips = meeting.speakers.length ? meeting.speakers.map(s => `<span class="chip">${escapeHtml(s.label)} · ${escapeHtml(s.name || "Unknown")}</span>`).join("") : '<span class="chip">No diarization labels recorded</span>';
  const entityChips = meeting.entities.slice(0, 12).map(e => `<span class="chip">${escapeHtml(e.name)}</span>`).join("");
  const driveLink = meeting.drive_url ? `<a href="${safeUrl(meeting.drive_url)}" target="_blank" rel="noreferrer">Open original audio ↗</a>` : "";
  reader.innerHTML = `<p class="reader-kicker">${escapeHtml(meeting.review_state)}</p><h3>${escapeHtml(meeting.title)}</h3><div class="reader-meta"><span>${displayDate(meeting.date)} ${escapeHtml(meeting.time)}</span><span>${duration(meeting.duration_sec)}</span><span>${escapeHtml(meeting.status)}</span></div><div class="reader-actions">${driveLink}</div><div class="speaker-list">${speakerChips}</div>${entityChips ? `<div class="entity-list">${entityChips}</div>` : ""}<div class="minutes">${escapeHtml(meeting.minutes || "Minutes have not been compiled yet.")}</div>`;
}

async function askArchive(event) {
  event.preventDefault();
  const answer = document.getElementById("answer");
  const question = document.getElementById("question").value;
  answer.className = "answer"; answer.textContent = "Searching the meeting record…";
  try {
    const data = await request("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, mode: document.getElementById("query-mode").value }) });
    answer.textContent = data.answer;
    const meta = document.createElement("small"); meta.className = "answer-meta";
    meta.textContent = `Retrieved in ${data.retrieval_sec.toFixed(1)}s · ${data.synthesized ? `written via ${data.provider}` : "written locally"}`;
    answer.append(meta);
  } catch (error) { answer.textContent = error.message; }
}

function escapeHtml(value) { return String(value || "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function safeUrl(value) { return String(value).startsWith("https://") ? value : "#"; }
document.getElementById("query-form").addEventListener("submit", askArchive);
document.getElementById("meeting-search").addEventListener("input", event => loadMeetings(event.target.value));
Promise.all([loadOverview(), loadMeetings()]).catch(error => { document.getElementById("meeting-list").innerHTML = `<p class="empty-list">${escapeHtml(error.message)}</p>`; document.getElementById("rag-health").textContent = "Dashboard needs attention"; });
